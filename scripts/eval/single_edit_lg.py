import numpy as np
import pandas as pd
import torch
import os
import argparse
from tqdm import tqdm
from rdkit import RDLogger, Chem
import yaml
from seq_graph_retro.utils.parse import get_reaction_info, extract_leaving_groups
from seq_graph_retro.utils.chem import apply_edits_to_mol
from seq_graph_retro.utils.edit_mol import canonicalize, generate_reac_set
from seq_graph_retro.models import EditLGSeparate
from seq_graph_retro.search import BeamSearch
from seq_graph_retro.molgraph import MultiElement
lg = RDLogger.logger()
lg.setLevel(4)
import logging
logging.basicConfig(filename='disc.log',level=logging.DEBUG)

try:
    ROOT_DIR = os.environ["SEQ_GRAPH_RETRO"]
    DATA_DIR = os.path.join(ROOT_DIR, "datasets", "uspto-50k")
    EXP_DIR = os.path.join(ROOT_DIR, "experiments")

except KeyError:
    ROOT_DIR = "./"
    DATA_DIR = os.path.join(ROOT_DIR, "datasets", "uspto-50k")
    EXP_DIR = os.path.join(ROOT_DIR, "local_experiments")

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
DEFAULT_TEST_FILE = f"{DATA_DIR}/canonicalized_test.csv"

def canonicalize_prod(p):
    pcanon = canonicalize(p)
    pmol = Chem.MolFromSmiles(pcanon)
    [atom.SetAtomMapNum(atom.GetIdx()+1) for atom in pmol.GetAtoms()]
    p = Chem.MolToSmiles(pmol)
    return p


def load_edits_model(args):
    edits_step = args.edits_step
    if edits_step is None:
        edits_step = "best_model"

    if "run" in args.edits_exp:
        # This addition because some of the new experiments were run using wandb
        edits_loaded = torch.load(os.path.join(args.exp_dir, "wandb", args.edits_exp, "files", edits_step + ".pt"), map_location=DEVICE)
        with open(f"{args.exp_dir}/wandb/{args.edits_exp}/files/config.yaml", "r") as f:
            tmp_loaded = yaml.load(f, Loader=yaml.FullLoader)

        model_name = tmp_loaded['model']['value']

    else:
        edits_loaded = torch.load(os.path.join(args.exp_dir, args.edits_exp,
                                  "checkpoints", edits_step + ".pt"),
                                  map_location=DEVICE)
        model_name = args.edits_exp.split("_")[0]

    return edits_loaded, model_name


def load_lg_model(args):
    lg_step = args.lg_step
    if lg_step is None:
        lg_step = "best_model"

    if "run" in args.lg_exp:
        # This addition because some of the new experiments were run using wandb
        lg_loaded = torch.load(os.path.join(args.exp_dir, "wandb", args.lg_exp, "files", lg_step + ".pt"), map_location=DEVICE)
        with open(f"{args.exp_dir}/wandb/{args.lg_exp}/files/config.yaml", "r") as f:
            tmp_loaded = yaml.load(f, Loader=yaml.FullLoader)

        model_name = tmp_loaded['model']['value']

    else:
        lg_loaded = torch.load(os.path.join(args.exp_dir, args.lg_exp,
                               "checkpoints", lg_step + ".pt"),
                                map_location=DEVICE)
        model_name = args.lg_exp.split("_")[0]

    return lg_loaded, model_name

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data_dir", default=DATA_DIR, help="Data directory")
    parser.add_argument("--exp_dir", default=EXP_DIR, help="Experiments directory.")
    parser.add_argument("--test_file", default=DEFAULT_TEST_FILE, help="Test file.")
    parser.add_argument("--edits_exp", default="SingleEdit_21-03-2020--20-33-05",
                        help="Name of edit prediction experiment.")
    parser.add_argument("--edits_step", default=None,
                        help="Checkpoint for edit prediction experiment.")
    parser.add_argument("--lg_exp", default="LGClassifier_02-04-2020--02-06-17",
                        help="Name of synthon completion experiment.")
    parser.add_argument("--lg_step", default=None,
                        help="Checkpoint for synthon completion experiment.")
    parser.add_argument("--beam_width", default=10, type=int, help="Beam width")
    parser.add_argument("--use_rxn_class", action='store_true', help="Whether to use reaction class.")
    parser.add_argument("--rxn_class_acc", action="store_true",
                        help="Whether to print reaction class accuracy.")
    args = parser.parse_args()

    #test_df = pd.read_csv(args.test_file)
    test_df = pd.read_csv("datasets/uspto-50k/smal_test.csv")
    edits_loaded, edit_net_name = load_edits_model(args)
    lg_loaded, lg_net_name = load_lg_model(args)

    edits_config = edits_loaded["saveables"]
    lg_config = lg_loaded['saveables']
    lg_toggles = lg_config['toggles']

    if 'tensor_file' in lg_config:
        if not os.path.isfile(lg_config['tensor_file']):
            if not lg_toggles.get("use_rxn_class", False):
                tensor_file = os.path.join(args.data_dir, "train/h_labels/without_rxn/lg_inputs.pt")
            else:
                tensor_file = os.path.join(args.data_dir, "train/h_labels/with_rxn/lg_inputs.pt")
            lg_config['tensor_file'] = tensor_file

    rm = EditLGSeparate(edits_config=edits_config, lg_config=lg_config, edit_net_name=edit_net_name,
                        lg_net_name=lg_net_name, device=DEVICE)
    rm.load_state_dict(edits_loaded['state'], lg_loaded['state'])
    rm.to(DEVICE)
    rm.eval()

    n_matched = np.zeros(args.beam_width)

    beam_model = BeamSearch(model=rm, beam_width=args.beam_width, max_edits=1)
    pbar = tqdm(list(range(len(test_df))))

    for idx in pbar:
        rxn_smi = test_df.loc[idx, 'reactants>reagents>production']
        r, p = rxn_smi.split(">>")

        rxn_class = test_df.loc[idx, 'class']

        if rxn_class != 'UNK':
            rxn_class = int(rxn_class)
        
        # Canonicalize the product and reactant sets, just for security.
        # The product is already canonicalized since the dataset we use is the canonicalized one.
        p = canonicalize_prod(p)
        r_can = canonicalize(r)
        rset = set(r_can.split("."))

        try:
            if lg_toggles.get("use_rxn_class", False):
                top_k_nodes = beam_model.run_search(p, max_steps=6, rxn_class=rxn_class)
                logging.info(f'top_k_nodes:{top_k_nodes}')
            else:
                top_k_nodes = beam_model.run_search(p, max_steps=6)
                logging.info(f'top_k_nodes:{top_k_nodes}')
            beam_matched = False
            for beam_idx, node in enumerate(top_k_nodes):
                logging.info(f'{beam_idx} -- node:{node}')
                pred_edit = node.edit
                pred_label = node.lg_groups
                logging.info(f'Pred_edit:{pred_edit}')
                logging.info(f'Pred_label:{pred_label}')
                if isinstance(pred_edit, list):
                    pred_edit = pred_edit[0]
                try:
                    pred_set = generate_reac_set(p, pred_edit, pred_label, verbose=False)
                except BaseException as e:
                    print(e, flush=True)
                    pred_set = None

                if pred_set == rset and not beam_matched:
                    n_matched[beam_idx] += 1
                    beam_matched = True

        except Exception as e:
            print(e)
            continue

        msg = 'average score'
        for beam_idx in [1, 3, 5, 10, 20, 50]:
            match_perc = np.sum(n_matched[:beam_idx]) / (idx + 1)
            msg += ', t%d: %.4f' % (beam_idx, match_perc)
        pbar.set_description(msg)
    logging.info(f'pred_set:{pred_set}')

if __name__ == "__main__":
    main()

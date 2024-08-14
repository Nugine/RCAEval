import argparse
import glob
import math
import itertools
import json
import os
import pickle
import warnings
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from os.path import abspath, basename, dirname, exists, join

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from causallearn.graph.GeneralGraph import GeneralGraph
from causallearn.score.LocalScoreFunction import local_score_BIC
from tqdm import tqdm

from RCAEval.benchmark.evaluation import Evaluator
from RCAEval.benchmark.metrics import F1, SHD, F1_Skeleton
from RCAEval.classes.graph import MemoryGraph, Node
from RCAEval.graph_heads import finalize_directed_adj
from RCAEval.io.time_series import drop_constant, drop_extra, drop_time
from RCAEval.utility import (
    dump_json,
    download_syn_rcd_dataset,
    download_syn_circa_dataset,
    download_syn_causil_dataset,
    is_py310,
    load_json,
)



if is_py310():
    from causallearn.search.ConstraintBased.FCI import fci
    from causallearn.search.ConstraintBased.PC import pc
    from causallearn.search.FCMBased.lingam import DirectLiNGAM, ICALiNGAM, VARLiNGAM
    from causallearn.search.ScoreBased.GES import ges
    from causallearn.utils.cit import chisq, fisherz, gsq, kci, mv_fisherz
    from RCAEval.graph_construction.granger import granger
    from RCAEval.graph_construction.pcmci import pcmci
    from RCAEval.graph_construction.cmlp import cmlp
    try:
        from RCAEval.graph_construction.dag_gnn import dag_gnn
        from RCAEval.graph_construction.dag_gnn import notears_low_rank as ntlr
        from RCAEval.graph_construction.notears import notears
    except Exception as e:
        print(e)
    
else:
    from RCAEval.graph_construction.fges import fges

AVAILABLE_METHODS = sorted(
    [
        "pc",
        "ppc",
        "pcmci",
        "fci",
        "fges",
        "notears",
        "ntlr",
        "DirectLiNGAM",
        "VARLiNGAM",
        "ICALiNGAM",
        "ges",
        "granger",
    ]
)


def adj2generalgraph(adj):
    G = GeneralGraph(nodes=[f"X{i + 1}" for i in range(len(adj))])
    for row_idx in range(len(adj)):
        for col_idx in range(len(adj)):
            if adj[row_idx, col_idx] == 1:
                G.add_directed_edge(f"X{col_idx + 1}", f"X{row_idx + 1}")
    return G


def score_g(Data, G, parameters=None):
    parameters = {"lambda_value": 0}

    score = 0
    for i, node in enumerate(G.get_nodes()):
        PA = G.get_parents(node)

        # for granger
        if len(PA) > 0 and isinstance(PA[0], str):
            pass  # already in str format
        else:
            PA = [p.name for p in PA]

        if len(PA) > 0 and isinstance(PA[0], str):
            # this is for FCI, bc it doesn't have node_names param
            # remove X from list ['X6', 'X10']
            PA = [int(p[1:]) - 1 for p in PA]


        delta_score = local_score_BIC(Data, i, PA, parameters)

        # delta_score is nan, ignore
        if np.isnan(delta_score):
            continue

        score = score + delta_score
    return score.sum()


def parse_args():
    parser = argparse.ArgumentParser(description="RCAEval evaluation")
    # for data
    parser.add_argument("-i", "--input-path", type=str, default="data", help="path to data")
    parser.add_argument(
        "-o", "--output-path", type=str, default="output", help="for results and reports"
    )
    # length
    parser.add_argument("--length", type=int, default=None, help="length of time series")
    parser.add_argument("--bic", default=None)

    # for method
    parser.add_argument("-m", "--model", type=str, default="pc_pagerank", help="func name")
    parser.add_argument("-t", "--test", type=str, default=None, help="granger test or pc test")
    parser.add_argument("-a", "--alpha", type=float, default=0.05)
    parser.add_argument("--tau", type=float, default=3)
    parser.add_argument("--stable", action="store_true")

    # for evaluation
    parser.add_argument("-w", "--worker-num", type=int, default=1, help="number of workers")
    parser.add_argument("--iter-num", type=int, default=1)
    parser.add_argument("--eval-step", type=int, default=None)

    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--useful", action="store_true")
    parser.add_argument("--tuning", action="store_true")
    parser.add_argument("--small", action="store_true")
    parser.add_argument("--large", action="store_true")

    args = parser.parse_args()

    assert args.alpha in [0.005, 0.01, 0.05, 0.1, 0.2]

    # assert args.tau in [3, 6, 10]

    # check if args.model is defined here
    if args.model not in globals():
        raise ValueError(f"{args.model=} not defined. Available: {AVAILABLE_METHODS}")

    if args.verbose:
        print(json.dumps(vars(args), indent=2, sort_keys=True))
    return args


args = parse_args()


# ==== PREPARE PATHS ====
output_path = TemporaryDirectory().name
report_path = join(output_path, "report.xlsx")
result_path = join(output_path, "results")
os.makedirs(result_path, exist_ok=True)


# ==== PROCESS TO GENERATE JSON ====
data_paths = list(glob.glob(os.path.join(args.input_path, "**/data.csv"), recursive=True))


def evaluate():
    eval_data = {
        "Case": [],
        "Precision": [],
        "Recall": [],
        "F1-Score": [],
        "Precision-Skel": [],
        "Recall-Skel": [],
        "F1-Skel": [],
        "BIC": [],
        "SHD": [],
    }

    # print("Evaluate...")
    for data_path in data_paths:
        # for circa and rcd
        if "_circa" in data_path or "_rcd" in data_path:
            num_node = int(basename(dirname(dirname(dirname(dirname(data_path))))))
            graph_idx = int(basename(dirname(dirname(dirname(data_path)))))
            case_idx = int(basename(dirname(data_path)))

        # === for CausIL
        if "syn_causil" in data_path:
            graph_idx = int(basename(dirname(data_path))[-1:])
            case_idx = 0

        # ===== READ RESULT =====
        est_graph_name = f"{graph_idx}_{case_idx}_est_graph.json"
        est_graph_path = join(result_path, est_graph_name)

        # for real circa pc
        # est_graph_name = f"../CIRCA/results/{graph_idx}_{case_idx}.json"
        # est_graph_path = est_graph_name

        if not exists(est_graph_path):
            continue
        est_graph = MemoryGraph.load(est_graph_path)

        # ====== READ TRUE GRAPH =====
        # for circa
        # /home/luan/ws/RCAEval/data/syn_circa/10/0/cases/0/data.csv
        if "_circa" in data_path:
            true_graph_path = join(dirname(dirname(dirname(data_path))), "graph.json")
            true_graph = MemoryGraph.load(true_graph_path)

        # for causil
        # /home/luan/ws/RCAEval/data/syn_causil/10_services/synthetic/Graph0/data.csv
        if "syn_causil" in data_path:
            dag_gt = pickle.load(open(join(dirname(data_path), "DAG.gpickle"), "rb"))
            true_graph = MemoryGraph(dag_gt)
            # draw_digraph(dag_gt, figsize=(8, 8))

        # for rcd
        if "_rcd" in data_path:
            dag_gt = pickle.load(
                open(join(dirname(dirname(dirname(data_path))), "g_graph.pkl"), "rb")
            )
            true_graph = MemoryGraph(dag_gt)
            true_graph = MemoryGraph.load(
                join(dirname(dirname(dirname(data_path))), "true_graph.json")
            )

        e = F1(true_graph, est_graph)
        e_skel = F1_Skeleton(true_graph, est_graph)
        shd = SHD(true_graph, est_graph)

        eval_data["Case"].append(est_graph_name)
        eval_data["Precision"].append(e["precision"])
        eval_data["Recall"].append(e["recall"])
        eval_data["F1-Score"].append(e["f1"])
        eval_data["Precision-Skel"].append(e_skel["precision"])
        eval_data["Recall-Skel"].append(e_skel["recall"])
        eval_data["F1-Skel"].append(e_skel["f1"])
        eval_data["SHD"].append(shd)

    avg_precision = np.mean(eval_data["Precision"])
    avg_recall = np.mean(eval_data["Recall"])
    avg_f1 = np.mean(eval_data["F1-Score"])
    avg_precision_skel = np.mean(eval_data["Precision-Skel"])
    avg_recall_skel = np.mean(eval_data["Recall-Skel"])
    avg_f1_skel = np.mean(eval_data["F1-Skel"])

    avg_shd = np.mean(eval_data["SHD"])
    avg_bic = np.mean(eval_data["BIC"])

    eval_data["Case"].insert(0, "Average")
    eval_data["Precision"].insert(0, avg_precision)
    eval_data["Recall"].insert(0, avg_recall)
    eval_data["F1-Score"].insert(0, avg_f1)
    eval_data["Precision-Skel"].insert(0, avg_precision_skel)
    eval_data["Recall-Skel"].insert(0, avg_recall_skel)
    eval_data["F1-Skel"].insert(0, avg_f1_skel)
    eval_data["BIC"].insert(0, avg_bic)
    eval_data["SHD"].insert(0, avg_shd)

    # print Average
    # print("================================")
    print(f"F1:   {avg_f1:.2f}")
    print(f"F1-S: {avg_f1_skel:.2f}")
    print(f"SHD:  {math.floor(avg_shd)}")

    eval_data.pop("BIC")

    report_df = pd.DataFrame(eval_data)
    report_df.to_excel(report_path, index=False)



def process(data_path):
    if "circa" in data_path:
        num_node = int(basename(dirname(dirname(dirname(dirname(data_path))))))
        graph_idx = int(basename(dirname(dirname(dirname(data_path)))))
        case_idx = int(basename(dirname(data_path)))

    if "causil" in data_path:
        num_node = int(basename(dirname(dirname(dirname(data_path)))).split("_")[0])
        graph_idx = int(basename(dirname(data_path))[-1:])
        case_idx = 0

    if "rcd" in data_path:
        num_node = int(basename(dirname(dirname(dirname(dirname(data_path))))))
        graph_idx = int(basename(dirname(dirname(dirname(data_path)))))
        case_idx = int(basename(dirname(data_path)))

    if "circa" in data_path: 
        data = pd.read_csv(data_path, header=None)
        data.header = list(map(str, range(0, data.shape[1])))
    else:
        data = pd.read_csv(data_path)


    # == PROCESS ==
    data = data.fillna(method="ffill")
    data = data.fillna(value=0)
    np_data = np.absolute(data.to_numpy().astype(float))

    if args.length is not None:
        np_data = np_data[: args.length, :]

    adj = []
    G = None

    st = datetime.now()
    try:
        if args.model == "pc":
            adj = pc(
                np_data,
                stable=False,
                show_progress=False,
            ).G.graph
        elif args.model == "fci":
            adj = fci(
                np_data,
                show_progress=False,
                verbose=False,
            )[0].graph
        elif args.model == "fges":
            adj = fges(pd.DataFrame(np_data))
        elif args.model == "ICALiNGAM":
            model = ICALiNGAM()
            model.fit(np_data)
            adj = model.adjacency_matrix_
            adj = adj.astype(bool).astype(int)
        elif args.model == "VARLiNGAM":
            raise NotImplementedError
        elif args.model == "DirectLiNGAM":
            model = DirectLiNGAM()
            model.fit(np_data)
            adj = model.adjacency_matrix_
            adj = adj.astype(bool).astype(int)
        elif args.model == "ges":
            record = ges(np_data)
            adj = record["G"].graph
        elif args.model == "granger":
            adj = granger(data, test=args.test, maxlag=args.tau, p_val_threshold=args.alpha)
        elif args.model == "pcmci":
            adj = pcmci(pd.DataFrame(np_data))
        elif args.model == "ntlr":
            adj = ntlr(pd.DataFrame(np_data))
        else:
            raise ValueError(f"{args.model=} not defined. Available: {AVAILABLE_METHODS}")

        if "circa" in data_path:
            est_graph = MemoryGraph.from_adj(
                adj, nodes=[Node("SIM", str(i)) for i in range(len(adj))]
            )
        else:
            est_graph = MemoryGraph.from_adj(adj, nodes=data.columns.to_list())

        est_graph.dump(join(result_path, f"{graph_idx}_{case_idx}_est_graph.json"))

    except Exception as e:
        raise e
        print(f"{args.model=} failed on {data_path=}")
        est_graph = MemoryGraph.from_adj([], nodes=[])
        est_graph.dump(join(result_path, f"{graph_idx}_{case_idx}_failed.json"))


start_time = datetime.now()

for data_path in tqdm(data_paths):
    output = process(data_path)

end_time = datetime.now()
time_taken = end_time - start_time
avg_speed = round(time_taken.total_seconds() / len(data_paths), 2)


evaluate()
print("Avg speed:", avg_speed)

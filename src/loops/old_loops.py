"""
Original source can be found here -> https://github.com/migalkin/StarE/blob/master/loops/loops.py
"""

from tqdm.autonotebook import tqdm
from typing import Callable

from utils.utils_mytorch import *
from .corruption import Corruption


def training_loop_gcn(
        epochs: int,
        data: dict,
        opt: torch.optim,
        model: Callable,
        device: torch.device = torch.device('cpu'),
        data_fn: Callable = SimplestSampler,
        val_testbench: Callable = default_eval,
        # test_testbench: Callable = default_eval,
        eval_every: int = 1,
        qualifier_aware: bool = False,
        grad_clipping: bool = True,
        early_stopping: int = 3,
        scheduler: Callable = None
    ):
    """
    A fn which can be used to train a language model.

    The model doesn't need to be an nn.Module,
        but have an eval (optional), a train and a predict function.

    Data should be a dict like so:
        {"train":{"x":np.arr, "y":np.arr}, "val":{"x":np.arr, "y":np.arr} }

    model must return both loss and y_pred

    :param epochs: integer number of epochs
    :param data: a dictionary which looks like {'train': train data}
    :param opt: torch optimizer
    :param model: a fn which is/can call forward of a nn module
    :param device: torch.device for making tensors
    :param data_fn: Something that can make iterators out of training data (think mytorch samplers)
    :param val_testbench: Function call to see generate all negs for all pos and get metrics in valid set
    :param eval_every: int which dictates after how many epochs should run testbenches
    """

    train_loss = []
    train_acc = []
    valid_acc = []
    valid_mrr = []
    valid_mr = []
    valid_hits_3, valid_hits_5, valid_hits_10 = [], [], []
    train_acc_bnchmk = []
    train_mrr_bnchmk = []
    train_hits_3_bnchmk, train_hits_5_bnchmk, train_hits_10_bnchmk = [], [], []

    # Epoch level
    for e in range(1, epochs + 1):
        per_epoch_loss = []

        with Timer() as timer:

            # Make data
            trn_dl = data_fn(data['train'])
            model.train()

            for batch in tqdm(trn_dl):
                opt.zero_grad()

                triples, labels = batch
                sub, rel = triples[:, 0], triples[:, 1]
                if qualifier_aware:
                    quals = triples[:, 2:]
                    _quals = torch.tensor(quals, dtype=torch.long, device=device)
                #sub, rel, obj, label = batch[:, 0], batch[:, 1], batch[:, 2], torch.ones((batch.shape[0], 1), dtype=torch.float)
                _sub = torch.tensor(sub, dtype=torch.long, device=device)
                _rel = torch.tensor(rel, dtype=torch.long, device=device)
                _labels = torch.tensor(labels, dtype=torch.float, device=device)

                pred = model(_sub, _rel, _quals)
                loss = model.loss(pred, _labels)

                per_epoch_loss.append(loss.item())
                
                loss.backward()

                if grad_clipping:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)

                opt.step()


        print(f"[Epoch: {e} ] Loss: {np.mean(per_epoch_loss)}", flush=True)
        train_loss.append(np.mean(per_epoch_loss))

        # Detailed metrics every n epochs
        if e % eval_every == 0 and e >= 1:
            with torch.no_grad():
                summary_val = val_testbench()
                per_epoch_vl_acc = summary_val['metrics']['hits_at 1']
                per_epoch_vl_mrr = summary_val['metrics']['mrr']
                per_epoch_vl_mr = summary_val['metrics']['mr']
                per_epoch_vl_hits_3 = summary_val['metrics']['hits_at 3']
                per_epoch_vl_hits_5 = summary_val['metrics']['hits_at 5']
                per_epoch_vl_hits_10 = summary_val['metrics']['hits_at 10']

                valid_acc.append(per_epoch_vl_acc)
                valid_mrr.append(per_epoch_vl_mrr)
                valid_mr.append(per_epoch_vl_mr)
                valid_hits_3.append(per_epoch_vl_hits_3)
                valid_hits_5.append(per_epoch_vl_hits_5)
                valid_hits_10.append(per_epoch_vl_hits_10)

                print("Epoch: %(epo)03d | Loss: %(loss).5f | "
                      "Vl_c: %(vlacc)0.5f | Vl_mrr: %(vlmrr)0.5f | Vl_mr: %(vlmr)0.5f | "
                      "Vl_h3: %(vlh3)0.5f | Vl_h5: %(vlh5)0.5f | Vl_h10: %(vlh10)0.5f | "
                      "time_trn: %(time).3f min"
                      % {'epo': e,
                         'loss': float(np.mean(per_epoch_loss)),
                         'vlacc': float(per_epoch_vl_acc),
                         'vlmrr': float(per_epoch_vl_mrr),
                         'vlmr': float(per_epoch_vl_mr),
                         'vlh3': float(per_epoch_vl_hits_3),
                         'vlh5': float(per_epoch_vl_hits_5),
                         'vlh10': float(per_epoch_vl_hits_10),
                         'time': timer.interval / 60.0
                         }, 
                         flush=True)

                ## Early stopping
                ## When not improve in last n validation stop
                if early_stopping is not None and len(valid_mrr) >= early_stopping and np.argmax(valid_mrr[-early_stopping:]) == 0:
                    print("Perforamance has not improved! Stopping now!")
                    break
                    
        else:
            # No test benches this time around
            print("Epoch: %(epo)03d | Loss: %(loss).5f | Time_Train: %(time).3f min" 
                  % {'epo': e, 'loss': float(np.mean(per_epoch_loss)), 'time': timer.interval / 60.0}, flush=True)
    
        if scheduler is not None:
            scheduler.step()


    # Print Test Results
    # print("\n\nTest Results:\n-------------\n")
    # test_results = test_testbench()

    return train_acc, train_loss, \
           train_acc_bnchmk, train_mrr_bnchmk, \
           train_hits_3_bnchmk, train_hits_5_bnchmk, train_hits_10_bnchmk, \
           valid_acc, valid_mrr, \
           valid_hits_3, valid_hits_5, valid_hits_10
import torch
import torch.nn as nn
import dgl.function as fn
from dgl.nn.pytorch import edge_softmax, GATConv
import numpy as np
from SourceCodeTools.graph.model.train.utils import get_num_batches

from os.path import join


def evaluate_no_classes(logits, labels):
    # pred = logits.argmax(1)
    pred = (logits > 0.).float() * 1.0
    acc = (pred == labels).float().mean()
    return acc


def track_best(epoch, loss, train_acc, val_acc, test_acc, best_val_acc, best_test_acc):
    if best_val_acc < val_acc:
        best_val_acc = val_acc
        best_test_acc = test_acc

    print('Epoch %d, Loss %.4f, Train Acc %.4f, Val Acc %.4f (Best %.4f), Test Acc %.4f (Best %.4f)' % (
        epoch,
        loss.item(),
        train_acc.item(),
        val_acc.item(),
        best_val_acc.item(),
        test_acc.item(),
        best_test_acc.item(),
    ))


def prepare_batch_no_classes(node_embeddings,
                             elem_embeder, # link_predictor,
                             indices,
                             batch_size,
                             negative_factor):
    K = negative_factor

    element_embeddings = elem_embeder(elem_embeder[indices])
    node_embeddings_batch = node_embeddings[indices]
    # positive_batch = torch.cat([node_embeddings_batch, element_embeddings], 1)
    labels_pos = torch.ones(batch_size)

    node_embeddings_neg_batch = node_embeddings_batch.repeat(K, 1)
    # negative_indices = torch.LongTensor(np.random.randint(low=0, high=elem_embeder.n_elements, size=batch_size * K))
    negative_indices = torch.LongTensor(elem_embeder.sample_negative(batch_size * K))
    negative_random = elem_embeder(negative_indices)
    # negative_batch = torch.cat([node_embeddings_neg_batch, negative_random], 1)
    labels_neg = torch.zeros(batch_size * K)

    nodes_batch = torch.cat([node_embeddings_batch, node_embeddings_neg_batch], 0)
    elements_batch = torch.cat([element_embeddings, negative_random], 0)
    # batch = torch.cat([positive_batch, negative_batch], 0)
    logits = (nodes_batch * elements_batch).sum(1)
    labels = torch.cat([labels_pos, labels_neg], 0)

    # logits = link_predictor(batch)

    return logits, labels


def final_evaluation_no_classes(model, elem_embeder, splits):
    train_idx, test_idx, val_idx = splits

    pool = set(elem_embeder.elements['id'].to_list())

    train_idx = np.fromiter(pool.intersection(train_idx), dtype=np.int64)
    test_idx = np.fromiter(pool.intersection(test_idx), dtype=np.int64)
    val_idx = np.fromiter(pool.intersection(val_idx), dtype=np.int64)

    node_embeddings = model()
    subsample = np.random.choice(train_idx, size=1000, replace=False)
    train_logits, train_labels = prepare_batch_no_classes(node_embeddings,
                                                          elem_embeder,
                                                          subsample,
                                                          subsample.size,
                                                          1)

    test_logits, test_labels = prepare_batch_no_classes(node_embeddings,
                                                        elem_embeder,
                                                        test_idx,
                                                        test_idx.size,
                                                        1)

    val_logits, val_labels = prepare_batch_no_classes(node_embeddings,
                                                      elem_embeder,
                                                      val_idx,
                                                      val_idx.size,
                                                      1)

    train_acc, val_acc, test_acc = evaluate_no_classes(train_logits, train_labels), \
                                   evaluate_no_classes(test_logits, test_labels), \
                                   evaluate_no_classes(val_logits, val_labels)

    # logp = nn.functional.log_softmax(train_logits, 1)
    # loss = nn.functional.nll_loss(logp, train_labels)
    loss = nn.functional.binary_cross_entropy_with_logits(train_logits, train_labels)

    scores = {
        "loss": loss.item(),
        "train_acc": train_acc.item(),
        "val_acc": val_acc.item(),
        "test_acc": test_acc.item(),
    }

    print('Final Eval Loss %.4f, Train Acc %.4f, Val Acc %.4f, Test Acc %.4f' % (
        scores["loss"],
        scores["train_acc"],
        scores["val_acc"],
        scores["test_acc"],
    ))

    return scores


def train_no_classes(MODEL_BASE, model, elem_embeder, splits, epochs):
    train_idx, test_idx, val_idx = splits

    pool = set(elem_embeder.elements['id'].to_list())

    print(f"Original set sizes: train {train_idx.size} test {test_idx.size} val {val_idx.size}")

    train_idx = np.fromiter(pool.intersection(train_idx), dtype=np.int64)
    test_idx = np.fromiter(pool.intersection(test_idx), dtype=np.int64)
    val_idx = np.fromiter(pool.intersection(val_idx), dtype=np.int64)

    print(f"Set sizes after filtering: train {train_idx.size} test {test_idx.size} val {val_idx.size}")

    # this is heldout because it was not used during training
    heldout_idx = test_idx.tolist() + val_idx.tolist()

    optimizer = torch.optim.Adagrad([
            {'params': model.parameters(), 'lr': 1e-2},
            {'params': elem_embeder.parameters(), 'lr': 1e-1}
        ], lr=0.01)

    best_val_acc = torch.tensor(0)
    best_test_acc = torch.tensor(0)

    batch_size = 4096
    K = 3  # negative oversampling factor

    num_batches, batch_size = get_num_batches(len(elem_embeder), batch_size)

    for epoch in range(epochs):

        # since we train in batches, we need to iterate over the nodes
        # since indexes are sampled randomly, it is a little bit hard to make sure we cover all data
        # instead, we sample nodes the same number of times that there are different nodes in the dataset,
        # hoping to cover all the data
        # num_batches = get_num_batches(len(elem_embeder), batch_size)
        # num_batches = len(elem_embeder) // batch_size
        for batch_ind in range(num_batches):

            node_embeddings = model()

            random_batch = np.random.choice(train_idx, size=batch_size)

            train_logits, train_labels = prepare_batch_no_classes(node_embeddings,
                                                                  elem_embeder,
                                                                  random_batch,
                                                                  batch_size,
                                                                  K)

            train_acc = evaluate_no_classes(train_logits, train_labels)

            # logp = nn.functional.log_softmax(train_logits, 1)
            # loss = nn.functional.nll_loss(logp, train_labels)
            loss = nn.functional.binary_cross_entropy_with_logits(train_logits, train_labels)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            if batch_ind % 1 == 0:
                print("\r%d/%d batches complete, acc: %.4f" % (batch_ind, num_batches, train_acc.item()), end="\n")

        test_logits, test_labels = prepare_batch_no_classes(node_embeddings,
                                                            elem_embeder,
                                                            test_idx,
                                                            test_idx.size,
                                                            1)

        val_logits, val_labels = prepare_batch_no_classes(node_embeddings,
                                                          elem_embeder,
                                                          val_idx,
                                                          val_idx.size,
                                                          1)

        test_acc, val_acc = evaluate_no_classes(test_logits, test_labels), \
                            evaluate_no_classes(val_logits, val_labels)

        track_best(epoch, loss, train_acc, val_acc, test_acc, best_val_acc, best_test_acc)

        torch.save({
            'm': model.state_dict(),
            'ee': elem_embeder.state_dict(),
            "epoch": epoch
        }, join(MODEL_BASE, "saved_state.pt"))

def training_procedure(dataset, model, params, EPOCHS, restore_state, MODEL_BASE):

    NODE_EMB_SIZE = 100
    ELEM_EMB_SIZE = 100

    m = model(dataset.g,
              num_classes=NODE_EMB_SIZE,
              produce_logits=False,
              **params)
    # get data for models with large number of classes, possibly several labels for
    # a single input id
    # TODO
    # Try other targets
    element_data = dataset.nodes[['global_graph_id', 'name']].rename(mapper={
        'global_graph_id': 'id'
    }, axis=1)
    element_data['dst'] = element_data['name'].apply(lambda name: name.split(".")[-1])
    from SourceCodeTools.graph.model.ElementEmbedder import ElementEmbedder
    ee = ElementEmbedder(element_data, ELEM_EMB_SIZE)

    assert ee.emb_size == m.emb_size, "Embedding sizes for GNN and ElementEmbedder should match"

    if restore_state:
        checkpoint = torch.load(join(MODEL_BASE, "saved_state.pt"))
        m.load_state_dict(checkpoint['m'])
        ee.load_state_dict(checkpoint['ee'])
        print(f"Restored from epoch {checkpoint['epoch']}")
        checkpoint = None

    # from LinkPredictor import LinkPredictor
    # lp = LinkPredictor(ee.emb_size + m.emb_size)

    try:
        train_no_classes(MODEL_BASE, m, ee, dataset.splits, EPOCHS)
    except KeyboardInterrupt:
        print("Training interrupted")
    finally:
        m.eval()
        ee.eval()
        # lp.eval()
        scores = final_evaluation_no_classes(m, ee, dataset.splits)

    return m, ee, scores

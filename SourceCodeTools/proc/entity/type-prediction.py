from __future__ import unicode_literals, print_function
import spacy
import sys, json, os
import pickle
from SourceCodeTools.proc.entity.util import inject_tokenizer, read_data, deal_with_incorrect_offsets
from spacy.gold import biluo_tags_from_offsets, offsets_from_biluo_tags

from spacy.gold import GoldParse
from spacy.scorer import Scorer

import random
from pathlib import Path
import spacy
from spacy.util import minibatch, compounding
import numpy as np
from copy import copy

from Embedder import Embedder
# from tf_model import create_batches

from tf_model import estimate_crf_transitions, TypePredictor, train

max_len = 400


class ClassWeightNormalizer:
    def __init__(self):
        self.class_counter = None

    def init(self, classes):
        import itertools
        from collections import Counter
        self.class_counter = Counter(c for c in itertools.chain.from_iterable(classes))
        total_count = sum(self.class_counter.values())
        self.class_weights = {key: total_count / val for key, val in self.class_counter.items()}

    def __getitem__(self, item):
        return self.class_weights[item]

    def get(self, item, default):
        return self.class_weights.get(item, default)


def evaluate(ner_model, examples):
    scorer = Scorer()
    for input_, annot in examples:
        doc_gold_text = ner_model.make_doc(input_)
        gold = GoldParse(doc_gold_text, entities=annot['entities'])
        pred_value = ner_model(input_)
        scorer.score(pred_value, gold)
    return {key: scorer.scores[key] for key in ['ents_p', 'ents_r', 'ents_f', 'ents_per_type']}
    return scorer.scores['ents_per_type']


# def isvalid(nlp, text, ents):
#     doc = nlp(text)
#     tags = biluo_tags_from_offsets(doc, ents)
#     if "-" in tags:
#         return False
#     else:
#         return True


def main_spacy(TRAIN_DATA, TEST_DATA, model, output_dir=None, n_iter=100):
    nlp = spacy.load(model)  # load existing spaCy model

    print("dealing with inconsistencies")
    TRAIN_DATA = deal_with_incorrect_offsets(TRAIN_DATA, nlp)
    TEST_DATA = deal_with_incorrect_offsets(TEST_DATA, nlp)
    print("done dealing with inconsistencies")

    # create the built-in pipeline components and add them to the pipeline
    # nlp.create_pipe works for built-ins that are registered with spaCy
    if "ner" not in nlp.pipe_names:
        ner = nlp.create_pipe("ner")
        nlp.add_pipe(ner, last=True)
    # otherwise, get it so we can add labels
    else:
        ner = nlp.get_pipe("ner")

    # add labels
    for _, annotations in TRAIN_DATA:
        for ent in annotations.get("entities"):
            ner.add_label(ent[2])

    # get names of other pipes to disable them during training
    other_pipes = [pipe for pipe in nlp.pipe_names if pipe != "ner"]
    with nlp.disable_pipes(*other_pipes):  # only train NER
        # reset and initialize the weights randomly – but only if we're
        # training a new model
        # if model is None:
        nlp.begin_training()
        for itn in range(n_iter):
            random.shuffle(TRAIN_DATA)
            losses = {}
            # batch up the examples using spaCy's minibatch
            batches = minibatch(TRAIN_DATA, size=compounding(4.0, 32.0, 1.001))
            for batch in batches:
                texts, annotations = zip(*batch)
                nlp.update(
                    texts,  # batch of texts
                    annotations,  # batch of annotations
                    drop=0.5,  # dropout - make it harder to memorise data
                    losses=losses,
                )
            print(f"{itn}:")
            print("\tLosses", losses)
            score = evaluate(nlp, TEST_DATA)
            if not os.path.isdir("models"):
                os.mkdir("models")
            nlp.to_disk(os.path.join("models", f"model_{itn}"))
            print("\t", score)


def prepare_data(sents, model_path):
    sents_w = []
    sents_t = []
    sents_r = []

    nlp = spacy.load(model_path)  # load existing spaCy model

    def try_int(val):
        try:
            return int(val)
        except:
            return val

    for s in sents:
        doc = nlp(s[0])
        ents = s[1]['entities']
        repl = s[1]['replacements']

        tokens = [t.text for t in doc]
        ents_tags = biluo_tags_from_offsets(doc, ents)
        repl_tags = biluo_tags_from_offsets(doc, repl)

        while "-" in ents_tags:
            ents_tags[ents_tags.index("-")] = "O"

        while "-" in repl_tags:
            repl_tags[repl_tags.index("-")] = "O"

        repl_tags = [try_int(t.split("-")[-1]) for t in repl_tags]

        sents_w.append(tokens)
        sents_t.append(ents_tags)
        sents_r.append(repl_tags)

    return sents_w, sents_t, sents_r


def load_pkl_emb(path):
    embedder = pickle.load(open(path, "rb"))
    if isinstance(embedder, list):
        embedder = embedder[-1]
    return embedder

def load_w2v_map(w2v_path):

    embs = []
    w_map = dict()

    with open(w2v_path) as w2v:
        n_vectors, n_dims = map(int, w2v.readline().strip().split())
        for ind in range(n_vectors):
            e = w2v.readline().strip().split()

            word = e[0]
            w_map[word] = len(w_map)

            embs.append(list(map(float, e[1:])))

    return Embedder(w_map, np.array(embs))

    # model = KeyedVectors.load_word2vec_format(model_p)
    # voc_len = len(model.vocab)
    #
    # vectors = np.zeros((voc_len, 100), dtype=np.float32)
    #
    # w2i = dict()
    #
    # for ind, word in enumerate(model.vocab.keys()):
    #     w2i[word] = ind
    #     vectors[ind, :] = model[word]
    #
    # # w2i["*P*"] = len(w2i)
    #
    # return model, w2i, vectors

def create_tag_map(sents):
    tags = set()

    for s in sents:
        tags.update(set(s))

    tagmap = dict(zip(tags, range(len(tags))))

    aid, iid = zip(*tagmap.items())
    inv_tagmap = dict(zip(iid, aid))

    return tagmap, inv_tagmap


def create_batches(batch_size, seq_len, sents, repl, tags, graphmap, wordmap, tagmap, class_weights):
    pad_id = len(wordmap)
    rpad_id = len(graphmap)
    n_sents = len(sents)

    b_sents = []
    b_repls = []
    b_tags = []
    b_cw = []
    b_lens = []

    for ind, (s, rr, tt)  in enumerate(zip(sents, repl, tags)):
        blank_s = np.ones((seq_len,), dtype=np.int32) * pad_id
        blank_r = np.ones((seq_len,), dtype=np.int32) * rpad_id
        blank_t = np.zeros((seq_len,), dtype=np.int32)
        blank_cw = np.ones((seq_len,), dtype=np.int32)

        int_sent = np.array([wordmap.get(w, pad_id) for w in s], dtype=np.int32)
        int_repl = np.array([graphmap.get(r, rpad_id) for r in rr], dtype=np.int32)
        int_tags = np.array([tagmap.get(t, 0) for t in tt], dtype=np.int32)
        int_cw = np.array([class_weights.get(t, 1.0) for t in tt], dtype=np.int32)

        blank_s[0:min(int_sent.size, seq_len)] = int_sent[0:min(int_sent.size, seq_len)]
        blank_r[0:min(int_sent.size, seq_len)] = int_repl[0:min(int_sent.size, seq_len)]
        blank_t[0:min(int_sent.size, seq_len)] = int_tags[0:min(int_sent.size, seq_len)]
        blank_cw[0:min(int_sent.size, seq_len)] = int_cw[0:min(int_sent.size, seq_len)]

        # print(int_sent[0:min(int_sent.size, seq_len)].shape)

        b_lens.append(len(s) if len(s) < seq_len else seq_len)
        b_sents.append(blank_s)
        b_repls.append(blank_r)
        b_tags.append(blank_t)
        b_cw.append(blank_cw)

    lens = np.array(b_lens, dtype=np.int32)
    sentences = np.stack(b_sents)
    replacements = np.stack(b_repls)
    pos_tags = np.stack(b_tags)
    cw = np.stack(b_cw)

    batch = []
    for i in range(n_sents // batch_size):
        batch.append((sentences[i * batch_size: i * batch_size + batch_size, :],
                      replacements[i * batch_size: i * batch_size + batch_size, :],
                      pos_tags[i * batch_size: i * batch_size + batch_size, :],
                      cw[i * batch_size: i * batch_size + batch_size, :],
                      lens[i * batch_size: i * batch_size + batch_size]))

    return batch


def parse_biluo(biluo):
    spans = []

    expected = {"B", "U", "0"}
    expected_tag = None

    c_start = 0

    for ind, t in enumerate(biluo):
        if t[0] not in expected:
            expected = {"B", "U", "0"}
            continue

        if t[0] == "U":
            c_start = ind
            c_end = ind + 1
            c_type = t.split("-")[1]
            spans.append((c_start, c_end, c_type))
            expected = {"B", "U", "0"}
            expected_tag = None
        elif t[0] == "B":
            c_start = ind
            expected = {"I", "L"}
            expected_tag = t.split("-")[1]
        elif t[0] == "I":
            if t.split("-")[1] != expected_tag:
                expected = {"B", "U", "0"}
                expected_tag = None
                continue
        elif t[0] == "L":
            if t.split("-")[1] != expected_tag:
                expected = {"B", "U", "0"}
                expected_tag = None
                continue
            c_end = ind + 1
            c_type = expected_tag
            spans.append((c_start, c_end, c_type))
            expected = {"B", "U", "0"}
            expected_tag = None
        elif t[0] == "0":
            expected = {"B", "U", "0"}
            expected_tag = None

    return spans



def scorer(pred, labels, inverse_tag_map, eps=1e-8):
    pred_biluo = [inverse_tag_map[p] for p in pred]
    labels_biluo = [inverse_tag_map[p] for p in labels]

    pred_spans = set(parse_biluo(pred_biluo))
    true_spans = set(parse_biluo(labels_biluo))

    tp = len(pred_spans.intersection(true_spans))
    fp = len(pred_spans - true_spans)
    fn = len(true_spans - pred_spans)

    precision = tp / (tp + fp + eps)
    recall = tp / (tp + fn + eps)
    f1 = 2 * precision * recall / (precision + recall + eps)

    return precision, recall, f1



def main_tf(TRAIN_DATA, TEST_DATA,
            tokenizer_path=None, graph_emb_path=None, word_emb_path=None,
            output_dir=None, n_iter=30, max_len=100):

    train_s, train_e, train_r = prepare_data(TRAIN_DATA, tokenizer_path)
    test_s, test_e, test_r = prepare_data(TEST_DATA, tokenizer_path)

    cw = ClassWeightNormalizer()
    cw.init(train_e)

    t_map, inv_t_map = create_tag_map(train_e)

    graph_emb = load_pkl_emb(graph_emb_path)
    word_emb = load_pkl_emb(word_emb_path)

    batches = create_batches(32, max_len, train_s, train_r, train_e, graph_emb.ind, word_emb.ind, t_map, cw)
    test_batch = create_batches(len(test_s), max_len, test_s, test_r, test_e, graph_emb.ind, word_emb.ind, t_map, cw)

    # transitions = estimate_crf_transitions(batches, len(t_map))

    model = TypePredictor(word_emb, graph_emb, train_embeddings=False,
                 h_sizes=[250], dense_size=100, num_classes=len(t_map),
                 seq_len=max_len, pos_emb_size=30, cnn_win_size=3)

    train(model=model, train_batches=batches, test_batches=test_batch, epochs=n_iter,
          scorer=lambda pred, true: scorer(pred, true, inv_t_map))



    print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Process some integers.')
    parser.add_argument('--tokenizer', dest='tokenizer', default=None,
                        help='')
    parser.add_argument('--data_path', dest='data_path', default=None,
                        help='Path to the file with nodes')
    parser.add_argument('--graph_emb_path', dest='graph_emb_path', default=None,
                        help='Path to the file with edges')
    parser.add_argument('--word_emb_path', dest='word_emb_path', default=None,
                        help='Path to the file with edges')

    args = parser.parse_args()

    model_path = sys.argv[1]
    data_path = sys.argv[2]
    output_dir = "model-final-ner"
    n_iter = 500

    allowed = {'str', 'bool', 'Optional', 'None', 'int', 'Any', 'Union', 'List', 'Dict', 'Callable', 'ndarray',
               'FrameOrSeries', 'bytes', 'DataFrame', 'Matcher', 'float', 'Tuple', 'bool_t', 'Description', 'Type'}

    # ent_types = []
    # for _, e in TRAIN_DATA:
    #     ee = [ent[2] for ent in e['entities']]
    #     ent_types += ee

    TRAIN_DATA, TEST_DATA = read_data(args.data_path, normalize=True, allowed=allowed, include_replacements=True)
    main_tf(TRAIN_DATA, TEST_DATA, args.tokenizer, graph_emb_path=args.graph_emb_path,
            word_emb_path=args.word_emb_path,
            output_dir=output_dir, n_iter=n_iter)
    # main_spacy(TRAIN_DATA, TEST_DATA, model=model_path,output_dir=output_dir, n_iter=n_iter)
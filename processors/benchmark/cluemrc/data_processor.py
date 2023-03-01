# -*- coding: utf-8 -*-
# @Time    : 2022/3/21 9:13 下午
# @Author  : JianingWang
# @File    : cmrc2018
import json
import torch
import os.path
import numpy as np
from dataclasses import dataclass
from typing import Optional
from collections import defaultdict
from transformers import PreTrainedTokenizerBase
from processors.ProcessorBase import CLSProcessor
from metrics import datatype2metrics
from processors.benchmark.cluemrc.clue_processor import clue_processors

from processors.benchmark.cluemrc.data_collator import DataCollatorForGlobalPointer


class CLUEMRCProcessor(CLSProcessor):
    def __init__(self,
                 data_args,
                 training_args,
                 model_args,
                 tokenizer=None,
                 post_tokenizer=False,
                 keep_raw_data=True):
        super().__init__(data_args,
                         training_args,
                         model_args,
                         tokenizer,
                         post_tokenizer=post_tokenizer,
                         keep_raw_data=keep_raw_data)
        param = {
            p.split('=')[0]: p.split('=')[1]
            for p in (data_args.user_defined).split(' ')
        }
        assert 'data_name' in param, "You must add one defined param 'data_name=xxx' in the user_defined parameter."
        self.data_name = param['data_name']
        self.train_file = os.path.join(data_args.data_dir, 'mrc_style',
                                       'train.json')
        if not os.path.exists(self.train_file):
            self.train_file = os.path.join(data_args.data_dir,
                                           'all_train.json')
        self.dev_file = os.path.join(data_args.data_dir, 'mrc_style',
                                     'dev.json')
        self.test_file = os.path.join(data_args.data_dir, 'mrc_style',
                                      'test.json')
        self.max_len = data_args.max_seq_length
        self.doc_stride = data_args.doc_stride
        self.sentence1_key = None

    def get_data_collator(self):
        pad_to_multiple_of_8 = self.training_args.fp16 and not self.data_args.pad_to_max_length
        return DataCollatorForGlobalPointer(
            self.tokenizer,
            pad_to_multiple_of=8 if pad_to_multiple_of_8 else None,
            pad_to_max_length=self.data_args.pad_to_max_length)

    def get_examples(self, set_type):
        if set_type == 'train':
            examples = self._create_examples(self._read_json(self.train_file),
                                             'train')
            # 使用 open data + 比赛训练数据直接训练
            # examples = self._create_examples(self._read_json(self.train_file) + self._read_json(self.dev_file) * 2, 'train')
            examples = examples[:self.data_args.max_train_samples]
            self.train_examples = examples
        elif set_type == 'dev':
            examples = self._create_examples(self._read_json(self.dev_file),
                                             'dev')
            examples = examples[:self.data_args.max_eval_samples]
            self.dev_examples = examples
        elif set_type == 'test':
            examples = self._create_examples(self._read_json(self.test_file),
                                             'test')
            examples = examples[:self.data_args.max_predict_samples]
            self.test_examples = examples
        return examples

    def _create_examples(self, lines, set_type):
        examples = []
        is_train = 0 if set_type == 'test' else 1
        for line in lines:
            id_ = line['ID']  # 原始数据的编号
            text = line['instruction']  # 原始文本+候选+模板形成的最终输入序列
            target = line['target']  # 目标答案
            start = line['start']  # 目标答案在输入序列的起始位置
            data_type = line['data_type']  # 该任务的类型
            if data_type == 'ner':
                new_start, new_end = [], []
                for t, entity_starts in zip(target, start):
                    for s in entity_starts:
                        new_start.append(s)
                        new_end.append(s + len(t))
                start, end = new_start, new_end
                target = '|'.join(target)
            else:
                start, end = [start], [start + len(target)]

            examples.append({
                'id': id_,
                'content': text,
                'start': start,
                'end': end,
                'target': target,
                'data_type': data_type,
                'is_train': is_train
            })

        return examples

    def set_config(self, config):
        config.ent_type_size = 1
        config.inner_dim = 64
        config.RoPE = True

    def build_preprocess_function(self):
        # Tokenize the texts
        tokenizer = self.tokenizer
        max_seq_length = self.data_args.max_seq_length

        def func(examples):
            # Tokenize
            tokenized_examples = tokenizer(
                examples['content'],
                truncation=True,
                max_length=max_seq_length,
                padding='max_length'
                if self.data_args.pad_to_max_length else False,
                return_offsets_mapping=True)
            # 确定label
            return tokenized_examples

        return func

    def get_predict_result(self, logits, examples):
        probs, indices = logits
        probs = probs.squeeze(1)  # topk结果的概率
        indices = indices.squeeze(1)  # topk结果的索引
        # print('probs=', probs) # [n, m]
        # print('indices=', indices) # [n, m]
        predictions = {}
        topk_predictions = {}
        for prob, index, example in zip(probs, indices, examples):
            data_type = example['data_type']
            id_ = example['id']
            index_ids = torch.Tensor([i for i in range(len(index))]).long()
            if data_type == 'ner':
                answer = []
                topk_answer = []
                # TODO 1. 调节阈值 2. 处理输出实体重叠问题
                entity_index = index[prob > 0.0]
                index_ids = index_ids[prob > 0.0]
                for ei, entity in enumerate(entity_index):
                    # 1D index转2D index
                    start_end = np.unravel_index(
                        entity, (self.data_args.max_seq_length,
                                 self.data_args.max_seq_length))
                    s = example['offset_mapping'][start_end[0]][0]
                    e = example['offset_mapping'][start_end[1]][1]
                    ans = example['content'][s:e]
                    if ans not in answer:
                        answer.append(ans)
                        topk_answer.append({
                            'answer': ans,
                            'prob': float(prob[index_ids[ei]]),
                            'pos': (s, e)
                        })
                predictions[id_] = answer
                topk_predictions[id_] = topk_answer
            else:
                # best_start_end = np.unravel_index(index[0],
                #                                   (self.data_args.max_seq_length, self.data_args.max_seq_length))
                best_start_end = np.unravel_index(index[0], (512, 512))
                s = example['offset_mapping'][best_start_end[0]][0]
                e = example['offset_mapping'][best_start_end[1]][1]
                answer = example['content'][s:e]
                predictions[id_] = answer

                topk_answer = []
                topk_index = index[prob > 0.0]
                index_ids = index_ids[prob > 0.0]
                # print('index_ids=', index_ids)
                for ei, index in enumerate(topk_index):
                    if ei > 6:
                        break
                    # 1D index转2D index
                    # start_end = np.unravel_index(index, (self.data_args.max_seq_length, self.data_args.max_seq_length))
                    start_end = np.unravel_index(index, (512, 512))
                    s = example['offset_mapping'][start_end[0]][0]
                    e = example['offset_mapping'][start_end[1]][1]
                    ans = example['content'][s:e]
                    topk_answer.append({
                        'answer': ans,
                        'prob': float(prob[index_ids[ei]]),
                        'pos': (s, e)
                    })
                topk_predictions[id_] = answer
                topk_predictions[id_] = topk_answer

        return predictions, topk_predictions

    def compute_metrics(self, eval_predictions):
        examples = self.raw_datasets['validation']
        golden, dataname_map, dataname_type = {}, defaultdict(list), {}
        predictions, _ = self.get_predict_result(eval_predictions[0], examples)
        for example in examples:
            data_type = example['data_type']
            dataname = '_'.join(example['id'].split('_')[:-1])
            if dataname not in dataname_type:
                dataname_type[dataname] = data_type
            id_ = example['id']
            dataname_map[dataname].append(id_)
            if data_type == 'ner':
                golden[id_] = example['target'].split('|')
            else:
                golden[id_] = example['target']

        all_metrics = {
            'macro_f1': 0.,
            'micro_f1': 0.,
            'eval_num': 0,
        }

        for dataname, data_ids in dataname_map.items():
            metric = datatype2metrics[dataname_type[dataname]]()
            gold = {k: v for k, v in golden.items() if k in data_ids}
            pred = {k: v for k, v in predictions.items() if k in data_ids}
            # pred = {"dev-{}".format(value['id']): value['label'] for value in predictions if "dev-{}".format(value['id']) in data_ids}
            score = metric.calc_metric(golden=gold, predictions=pred)
            acc, f1 = score['acc'], score['f1']
            if len(gold) != len(pred) or len(gold) < 20:
                print(dataname, dataname_type[dataname], round(acc, 4),
                      len(gold), len(pred), data_ids)
            all_metrics['macro_f1'] += f1
            all_metrics['micro_f1'] += f1 * len(data_ids)
            all_metrics['eval_num'] += len(data_ids)
            all_metrics[dataname] = round(acc, 4)
        all_metrics['macro_f1'] = round(
            all_metrics['macro_f1'] / len(dataname_map), 4)
        all_metrics['micro_f1'] = round(
            all_metrics['micro_f1'] / all_metrics['eval_num'], 4)
        return all_metrics

    def save_result(self, logits, label_ids):
        examples = self.raw_datasets['test']
        predicts, topk_predictions = self.get_predict_result(logits, examples)
        clue_processor = clue_processors[self.data_name]()
        label2word = clue_processor.get_verbalizers()
        word2label = {v: k for k, v in label2word.items()}

        ### submit 格式转换为clue的
        answer = list()
        for k, v in predicts.items():
            if v not in word2label.keys():
                res = ''
                print('unknow answer: {}'.format(v))
            else:
                res = word2label[v]
            answer.append({'id': int(k.split('-')[1]), 'label': res})

        outfile = os.path.join(self.training_args.output_dir, 'answer.json')
        with open(outfile, 'w', encoding='utf8') as f:
            #     json.dump(predicts, f, ensure_ascii=False, indent=2)
            for res in answer:
                f.write('{}\n'.format(str(res)))

        output_submit_file = os.path.join(self.training_args.output_dir,
                                          'answer.json')
        # 保存标签结果
        with open(output_submit_file, 'w') as writer:
            for i, pred in enumerate(answer):
                json_d = {}
                json_d['id'] = i
                json_d['label'] = pred['label']
                writer.write(json.dumps(json_d) + '\n')

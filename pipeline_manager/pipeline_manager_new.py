# Copyright 2017 Neural Networks and Deep Learning lab, MIPT
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os

from time import time
from tqdm import tqdm
from os.path import join
from pathlib import Path
from shutil import rmtree
from psutil import cpu_count
from datetime import datetime
from copy import copy
from multiprocessing import Pool
from typing import Union, Dict

from pipeline_manager.pipegen import PipeGen
from pipeline_manager.observer_new import Observer
from pipeline_manager.utils_new import normal_time
from pipeline_manager.utils_new import get_num_gpu
from pipeline_manager.utils_new import results_visualization, get_available_gpus

from deeppavlov.core.common.file import read_json
from deeppavlov.core.common.errors import ConfigError
from deeppavlov.core.common.prints import RedirectedPrints
from deeppavlov.core.data.data_fitting_iterator import DataFittingIterator
from deeppavlov.core.commands.train import train_evaluate_model_from_config
from deeppavlov.core.commands.train import read_data_by_config, get_iterator_from_config


def unpack_args(func):
    from functools import wraps

    @wraps(func)
    def wrapper(args):
        if isinstance(args, dict):
            return func(**args)
        else:
            return func(*args)
    return wrapper


class PipelineManager:
    """
    The class implements the functions of automatic pipeline search and search for hyperparameters.

    Args:
        config_path: path to config file.

    Attributes:
        exp_name: name of the experiment.
        date: date of the experiment.
        info: some additional information that you want to add to the log, the content of the dictionary
              does not affect the algorithm
        root: root path, the root path where the report will be generated and saved checkpoints
        sample_num: determines the number of generated pipelines, if hyper_search == random.
        target_metric: The metric name on the basis of which the results will be sorted when the report
                       is generated. The default value is None, in this case the target metric is taken the
                       first name from those names that are specified in the config file. If the specified metric
                       is not contained in DeepPavlov will be called error.
        observer: A special class that collects auxiliary statistics and results during training, and stores all
                the collected data in a separate log.
        plot: boolean trigger, which determines whether to draw a graph of results or not
        pipeline_generator: A special class that generates configs for training.
    """
    def __init__(self, config_path: Union[str, Dict, Path]):
        """
        Initialize observer, read input args, builds a directory tree, initialize date.
        """
        if isinstance(config_path, str):
            self.exp_config = read_json(config_path)
        elif isinstance(config_path, Path):
            self.exp_config = read_json(config_path)
        else:
            self.exp_config = config_path

        self.exp_name = self.exp_config['enumerate'].get('exp_name', 'experiment')
        self.date = self.exp_config['enumerate'].get('date', datetime.now().strftime('%Y-%m-%d'))
        self.info = self.exp_config['enumerate'].get('info')
        self.root = self.exp_config['enumerate'].get('root', 'download/experiments/')
        self.plot = self.exp_config['enumerate'].get('plot', False)
        self.save_best = self.exp_config['enumerate'].get('save_best', False)
        self.do_test = self.exp_config['enumerate'].get('do_test', False)
        self.cross_validation = self.exp_config['enumerate'].get('cross_val', False)
        self.k_fold = self.exp_config['enumerate'].get('k_fold', 5)
        self.sample_num = self.exp_config['enumerate'].get('sample_num', 10)
        self.target_metric = self.exp_config['enumerate'].get('target_metric')
        self.multiprocessing = self.exp_config['enumerate'].get('multiprocessing', True)
        self.max_num_workers_ = self.exp_config['enumerate'].get('max_num_workers')
        self.use_all_gpus = self.exp_config['enumerate'].get('use_all_gpus', False)
        self.use_multi_gpus = self.exp_config['enumerate'].get('use_multi_gpus')
        self.memory_fraction = self.exp_config['enumerate'].get('gpu_memory_fraction', 1.0)

        self.pipeline_generator = None
        self.gen_len = 0

        # observer initialization
        self.save_path = join(self.root, self.date, self.exp_name, 'checkpoints')
        self.observer = Observer(self.exp_name, self.root, self.info, self.date, self.plot, self.save_best)

        # multiprocessing
        if self.use_multi_gpus and self.use_all_gpus:
            raise ValueError("Parameters 'use_all_gpus' and 'use_multi_gpus' can not simultaneously be not None.")

        self.max_num_workers = None
        self.available_gpu = None
        if self.multiprocessing:
            self.prepare_multiprocess()

        # write time of experiment start
        self.start_exp = time()
        # start test
        if self.do_test:
            self.dataset_composition = dict(train=False, valid=False, test=False)
            self.test()

    def prepare_multiprocess(self):
        cpu_num = cpu_count()
        gpu_num = get_num_gpu()

        try:
            visible_gpu = [int(q) for q in os.environ['CUDA_VISIBLE_DEVICES'].split(',')]
            os.environ['CUDA_VISIBLE_DEVICES'] = ""
        except KeyError:
            visible_gpu = []

        if self.max_num_workers_ is not None and self.max_num_workers_ < 1:
            raise ConfigError("The number of workers must be at least equal to one. "
                              "Please check 'max_num_workers' parameter in config.")

        if self.use_all_gpus:
            if self.max_num_workers_ is None:
                self.available_gpu = get_available_gpus(gpu_fraction=self.memory_fraction)
                if len(visible_gpu) != 0:
                    self.available_gpu = list(set(self.available_gpu) & set(visible_gpu))

                if len(self.available_gpu) == 0:
                    raise ValueError("GPU with numbers: ({}) are busy.".format(set(visible_gpu)))
                elif len(self.available_gpu) < len(visible_gpu):
                    print("PipelineManagerWarning: 'CUDA_VISIBLE_DEVICES' = ({0}), "
                          "but only {1} are available.".format(visible_gpu, self.available_gpu))

                if int(cpu_num * 0.7) > len(self.available_gpu):
                    self.max_num_workers = len(self.available_gpu)
                else:
                    self.max_num_workers = int(cpu_num * 0.7)
            else:
                if self.max_num_workers_ > gpu_num:
                    self.max_num_workers = gpu_num
                    self.available_gpu = get_available_gpus(gpu_fraction=self.memory_fraction)
                    if len(visible_gpu) != 0:
                        self.available_gpu = list(set(self.available_gpu) & set(visible_gpu))

                    if len(self.available_gpu) == 0:
                        raise ValueError("GPU with numbers: ({}) are busy.".format(set(visible_gpu)))
                else:
                    self.available_gpu = get_available_gpus(num_gpus=self.max_num_workers_,
                                                            gpu_fraction=self.memory_fraction)
                    if len(visible_gpu) != 0:
                        self.available_gpu = list(set(self.available_gpu) & set(visible_gpu))

                    if len(self.available_gpu) == 0:
                        raise ValueError("GPU with numbers: ({}) are busy.".format(set(visible_gpu)))

                    self.max_num_workers = len(self.available_gpu)

        elif self.use_multi_gpus:
            if len(visible_gpu) != 0:
                self.use_multi_gpus = list(set(self.use_multi_gpus) & set(visible_gpu))

            if len(self.use_multi_gpus) == 0:
                raise ValueError("GPU numbers in 'use_multi_gpus' and 'CUDA_VISIBLE_DEVICES' "
                                 "has not intersections".format(set(visible_gpu)))

            self.available_gpu = get_available_gpus(gpu_select=self.use_multi_gpus, gpu_fraction=self.memory_fraction)

            if len(self.available_gpu) == 0:
                raise ValueError("All GPU from 'use_multi_gpus' are busy. "
                                 "GPU numbers ({});".format(self.use_multi_gpus))

            if not self.max_num_workers_:
                self.max_num_workers = len(self.available_gpu)
            else:
                if self.max_num_workers_ > len(self.available_gpu):
                    self.max_num_workers = len(self.available_gpu)
                else:
                    self.max_num_workers = self.max_num_workers_
                    self.available_gpu = self.available_gpu[0:self.max_num_workers_]

        else:
            if self.max_num_workers_ is not None and self.max_num_workers_ > cpu_num:
                print("PipelineManagerWarning: parameter 'max_num_workers'={0}, "
                      "but amounts of cpu is {1}. The {1} will be assigned to 'max_num_workers' "
                      "as default.".format(self.max_num_workers_, cpu_num))
                self.max_num_workers_ = cpu_num

            self.max_num_workers = self.max_num_workers_

    @staticmethod
    @unpack_args
    def train_pipe(pipe, i, observer, target_metric, gpu, gpu_ind=0):
        # modify project environment
        if gpu:
            os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu_ind)
        else:
            os.environ['CUDA_VISIBLE_DEVICES'] = ''

        if target_metric is None:
            if observer.log['experiment_info']['metrics'] is None:
                observer.log['experiment_info']['metrics'] = copy(pipe['train']['metrics'])
            if observer.log['experiment_info']['target_metric'] is None:
                if isinstance(pipe['train']['metrics'][0], str):
                    observer.log['experiment_info']['target_metric'] = copy(pipe['train']['metrics'][0])
                else:
                    observer.log['experiment_info']['target_metric'] = copy(pipe['train']['metrics'][0]['name'])

        observer.pipe_ind = i + 1
        observer.pipe_conf = copy(pipe['chainer']['pipe'])
        dataset_name = copy(pipe['dataset_reader']['data_path'])
        observer.dataset = dataset_name
        observer.batch_size = pipe['train'].get('batch_size', "None")

        # start pipeline time
        pipe_start = time()
        # create save path and save folder
        # TODO check logic, where folder already creates
        try:
            save_path = observer.create_save_folder(dataset_name)
        except FileExistsError:
            save_path = join(observer.save_path, dataset_name, "pipe_{}".format(i))

        # run pipeline train with redirected output flow
        with RedirectedPrints(new_target=open(join(save_path, "out.txt"), "w")):
            results = train_evaluate_model_from_config(pipe, to_train=True, to_validate=True)

        # add results and pipe time to log
        observer.pipe_time = normal_time(time() - pipe_start)
        observer.pipe_res = results

        # save config in checkpoint folder
        observer.save_config(pipe, dataset_name)

        # update logger
        observer.update_log()
        observer.write()

        return None

    def gpu_gen(self, gpu=False):
        if gpu:
            for i, pipe_conf in enumerate(self.pipeline_generator()):
                gpu_ind = i - (i // len(self.available_gpu)) * len(self.available_gpu)
                yield (pipe_conf, i, self.observer, self.target_metric, True, gpu_ind)
        else:
            for i, pipe_conf in enumerate(self.pipeline_generator()):
                yield (pipe_conf, i, self.observer, self.target_metric, False)

    def run(self):
        """
        Initializes the pipeline generator and runs the experiment. Creates a report after the experiments.
        """
        # create the pipeline generator
        self.pipeline_generator = PipeGen(self.exp_config, self.save_path, sample_num=self.sample_num, test_mode=False)
        self.gen_len = self.pipeline_generator.length

        # Start generating pipelines configs
        print('[ Experiment start - {0} pipes, will be run]'.format(self.gen_len))
        self.observer.log['experiment_info']['number_of_pipes'] = self.gen_len

        if self.multiprocessing:
            # start multiprocessing
            workers = Pool(self.max_num_workers)

            if self.available_gpu is None:
                results = list(tqdm(workers.imap_unordered(self.train_pipe, [x for x in self.gpu_gen(gpu=False)]),
                                    total=self.gen_len))
                workers.close()
                workers.join()
            else:
                results = list(tqdm(workers.imap_unordered(self.train_pipe, [x for x in self.gpu_gen(gpu=True)]),
                                    total=self.gen_len))
                workers.close()
                workers.join()
        else:
            for i, pipe in enumerate(tqdm(self.pipeline_generator(), total=self.gen_len)):
                if self.available_gpu is None:
                    self.train_pipe(pipe, i, self.observer, self.target_metric, False)
                else:
                    gpu_ind = i - (i // len(self.available_gpu)) * len(self.available_gpu)
                    self.train_pipe(pipe, i, self.observer, self.target_metric, True, gpu_ind)

        # save log
        self.observer.log['experiment_info']['full_time'] = normal_time(time() - self.start_exp)

        # delete all checkpoints and save only best pipe
        if self.save_best:
            self.observer.save_best_pipe()

        print("[ End of experiment ]")
        # visualization of results
        print("[ Create an experiment report ... ]")
        path = join(self.root, self.date, self.exp_name)
        results_visualization(path, self.plot)
        print("[ Report created ]")
        return None

    def test(self):
        """
        Initializes the pipeline generator with tiny data and runs the test of experiment.
        """
        # create the pipeline generator
        pipeline_generator = PipeGen(self.exp_config, self.save_path, sample_num=self.sample_num, test_mode=True)
        len_gen = pipeline_generator.length

        # Start generating pipelines configs
        print('[ Test start - {0} pipes, will be run]'.format(len_gen))
        for i, pipe in enumerate(tqdm(pipeline_generator(), total=len_gen)):
            data_iterator_i = self.test_dataset_reader_and_iterator(pipe, i)
            results = train_evaluate_model_from_config(pipe, iterator=data_iterator_i, to_train=True, to_validate=False)
            del results

        # del all tmp files in save path
        rmtree(join(self.save_path, "tmp"))
        print('[ The test was successful ]')
        return None

    def test_dataset_reader_and_iterator(self, config, i):
        # create and test data generator and data iterator
        data = read_data_by_config(config)
        if i == 0:
            for dtype in self.dataset_composition.keys():
                if len(data.get(dtype, [])) != 0:
                    self.dataset_composition[dtype] = True
        else:
            for dtype in self.dataset_composition.keys():
                if len(data.get(dtype, [])) == 0 and self.dataset_composition[dtype]:
                    raise ConfigError("The file structure in the {0} dataset differs "
                                      "from the rest datasets.".format(config['dataset_reader']['data_path']))

        iterator = get_iterator_from_config(config, data)
        if isinstance(iterator, DataFittingIterator):
            raise ConfigError("Instance of a class 'DataFittingIterator' is not supported.")
        else:
            if config.get('train', None):
                if config['train']['test_best'] and len(iterator.data['test']) == 0:
                    raise ConfigError("The 'test' part of dataset is empty, but 'test_best' in train config is 'True'."
                                      " Please check the dataset_iterator config.")

                if (config['train']['validate_best'] or config['train'].get('val_every_n_epochs', False) > 0) and \
                        len(iterator.data['valid']) == 0:
                    raise ConfigError("The 'valid' part of dataset is empty, but 'valid_best' in train config is 'True'"
                                      " or 'val_every_n_epochs' > 0. Please check the dataset_iterator config.")
            else:
                if len(iterator.data['test']) == 0:
                    raise ConfigError("The 'test' part of dataset is empty as a 'train' part of config file, "
                                      "but default value of 'test_best' is 'True'. "
                                      "Please check the dataset_iterator config.")

        # get a tiny data from dataset
        if len(iterator.data['train']) <= 100:
            print("!!!!!!!!!!!!! WARNING !!!!!!!!!!!!! Length of 'train' part dataset <= 100. "
                  "Please check the dataset_iterator config")
            tiny_train = copy(iterator.data['train'])
        else:
            tiny_train = copy(iterator.data['train'][:10])
        iterator.train = tiny_train

        if len(iterator.data['valid']) <= 20:
            tiny_valid = copy(iterator.data['valid'])
        else:
            tiny_valid = copy(iterator.data['valid'][:5])
        iterator.valid = tiny_valid

        if len(iterator.data['test']) <= 20:
            tiny_test = copy(iterator.data['test'])
        else:
            tiny_test = copy(iterator.data['test'][:5])
        iterator.test = tiny_test

        iterator.data = {'train': tiny_train,
                         'valid': tiny_valid,
                         'test': tiny_test,
                         'all': tiny_train + tiny_valid + tiny_test}

        return iterator
import sys
import itertools
import pandas as pd
from sklearn.experimental import enable_halving_search_cv # noqa
from sklearn.model_selection import HalvingGridSearchCV
from sklearn.model_selection import GridSearchCV, KFold, train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score  
import warnings
import time
from tempfile import mkdtemp
from shutil import rmtree
from joblib import Memory
from read_file import read_file
import pdb
import numpy as np
import json
import os
import inspect
from utils import jsonify

def evaluate_model(dataset, results_path, random_state, est_name, est, 
                   hyper_params, complexity, model, test=False, 
                   target_noise=0.0, feature_noise=0.0, 
                   n_samples=10000, scale_x = True, scale_y = True,
                   pre_train=None):

    print(40*'=','Evaluating '+est_name+' on ',dataset,40*'=',sep='\n')

    np.random.seed(random_state)
    if hasattr(est, 'random_state'):
        est.random_state = random_state

    ##################################################
    # setup data
    ##################################################
    features, labels, feature_names = read_file(dataset)


    # generate train/test split
    X_train, X_test, y_train, y_test = train_test_split(features, labels,
                                                    train_size=0.75,
                                                    test_size=0.25,
                                                    random_state=random_state)

    # if dataset is large, subsample the training set 
    if n_samples > 0 and len(labels) > n_samples:
        print('subsampling training data from',len(X_train),'to',n_samples)
        sample_idx = np.random.choice(np.arange(len(X_train)), size=n_samples)
        X_train = X_train[sample_idx]
        y_train = y_train[sample_idx]

    # scale and normalize the data
    if scale_x:
        print('scaling X')
        sc_X = StandardScaler() 
        X_train_scaled = sc_X.fit_transform(X_train)
        X_test_scaled = sc_X.transform(X_test)
    else:
        X_train_scaled = X_train
        X_test_scaled = X_test

    if scale_y:
        print('scaling y')
        sc_y = StandardScaler()
        y_train_scaled = sc_y.fit_transform(y_train.reshape(-1,1)).flatten()
    else:
        y_train_scaled = y_train

    # add noise to the target
    if target_noise > 0:
        print('adding',target_noise,'noise to target')
        y_train_scaled += np.random.normal(0, 
                            target_noise*np.linalg.norm(y_train_scaled),
                            size=len(y_train_scaled))
    # add noise to the features
    if feature_noise > 0:
        print('adding',target_noise,'noise to features')
        X_train_scaled = np.array([x + np.random.normal(0, 
                                            feature_noise*np.linalg.norm(x), 
                                            size=len(x))
                            for x in X_train_scaled.T]).T

    # run any method-specific pre_train routines
    if pre_train:
        pre_train(est, X_train_scaled, y_train_scaled)

    print('X_train:',X_train_scaled.shape)
    print('y_train:',y_train_scaled.shape)
    
    ################################################## 
    # define CV strategy for hyperparam tuning
    ################################################## 
    # define a test mode with fewer splits, no hyper_params, and few iterations
    if test:
        print('test mode enabled')
        n_splits = 2
        hyper_params = {}
        print('hyper_params set to',hyper_params)
        for genname in ['generations','gens','g','itrNum','treeNum']:
            if hasattr(est, genname):
                print('setting',genname,'=2 for test')
                setattr(est, genname, 2)
        if hasattr(est, 'popsize'):
            print('setting popsize=5 for test')
            est.popsize = 20 
        if hasattr(est, 'val'):
            print('setting val=1 for test')
            est.val = 1
        # deep sr setting
        if hasattr(est, 'config'):
            est.config['training']['n_samples'] = 10
            est.config['training']['batch_size'] = 10
            est.config['training']['hof'] = 5
    else:
        n_splits = 5

    cv = KFold(n_splits=n_splits, shuffle=True,random_state=random_state)

    grid_est = HalvingGridSearchCV(est,cv=cv, param_grid=hyper_params,
            verbose=2,n_jobs=1,scoring='r2',error_score=0.0)

    ################################################## 
    # Fit models
    ################################################## 
    t0p = time.process_time()
    t0t = time.time()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        grid_est.fit(X_train_scaled, y_train_scaled)
    process_time = time.process_time() - t0p
    time_time = time.time() - t0t
    print('Training time measures:',process_time, time_time)
    best_est = grid_est.best_estimator_
    # best_est = grid_est
    
    ##################################################
    # store results
    ##################################################
    dataset_name = dataset.split('/')[-1][:-7]
    results = {
        'dataset':dataset_name,
        'algorithm':est_name,
        'params':{k:v for k,v in best_est.get_params().items() 
                  if any(isinstance(v, t) for t in [bool,int,float,str])},
        'random_state':random_state,
        'process_time': process_time, 
        'time_time': time_time, 
    }

    # get the size of the final model
    if complexity == None:
        results['model_size'] = int(features.shape[1])
    else:
        results['model_size'] = int(complexity(best_est))

    # get the final symbolic model as a string
    if model == None:
        results['symbolic_model'] = 'not implemented'
    else:
        if 'X' in inspect.signature(model).parameters.keys():
            results['symbolic_model'] = model(best_est, X_train_scaled)
        else:
            results['symbolic_model'] = model(best_est)

    # scores
    pred = grid_est.predict

    for fold, target, X in zip(['train','test'],
                               [y_train, y_test], 
                               [X_train_scaled, X_test_scaled]
                              ):
        for score, scorer in [('mse',mean_squared_error),
                              ('mae',mean_absolute_error),
                              ('r2', r2_score)
                             ]:
            y_pred = sc_y.inverse_transform(pred(X)) if scale_y else pred(X)
            results[score + '_' + fold] = scorer(target, y_pred) 
    
    ##################################################
    # write to file
    ##################################################
    if not os.path.exists(results_path):
        os.makedirs(results_path)

    save_file = (results_path + '/' + dataset_name + '_' + est_name + '_' 
                 + str(random_state))
    print('save_file:',save_file)

    with open(save_file + '.json', 'w') as out:
        json.dump(jsonify(results), out, indent=4)

    # store CV detailed results
    cv_results = grid_est.cv_results_
    cv_results['random_state'] = random_state

    with open(save_file + '_cv_results.json', 'w') as out:
        json.dump(jsonify(cv_results), out, indent=4)

################################################################################
# main entry point
################################################################################
import argparse
import importlib

if __name__ == '__main__':

    # parse command line arguments
    parser = argparse.ArgumentParser(
        description="Evaluate a method on a dataset.", add_help=False)
    parser.add_argument('INPUT_FILE', type=str,
                        help='Data file to analyze; ensure that the '
                        'target/label column is labeled as "class".')    
    parser.add_argument('-h', '--help', action='help',
                        help='Show this help message and exit.')
    parser.add_argument('-ml', action='store', dest='ALG',default=None,type=str, 
            help='Name of estimator (with matching file in methods/)')
    parser.add_argument('-results_path', action='store', dest='RDIR',
                        default='results_test', type=str, 
                        help='Name of save file')
    parser.add_argument('-seed', action='store', dest='RANDOM_STATE',
                        default=42, type=int, help='Seed / trial')
    parser.add_argument('-test',action='store_true', dest='TEST', 
                       help='Used for testing a minimal version')
    parser.add_argument('-target_noise',action='store',dest='Y_NOISE',
                        default=0.0, type=float, help='Gaussian noise to add'
                        'to the target')
    parser.add_argument('-feature_noise',action='store',dest='X_NOISE',
                        default=0.0, type=float, help='Gaussian noise to add'
                        'to the target')

    args = parser.parse_args()
    # import algorithm 
    print('import from','methods.'+args.ALG)
    algorithm = importlib.__import__('methods.'+args.ALG,
                                     globals(),
                                     locals(),
                                     ['*']
                                    )
    if args.ALG == 'mrgp':
        algorithm.est.dataset=args.INPUT_FILE.split('/')[-1][:-7]

    print('algorithm:',algorithm.est)
    print('hyperparams:',algorithm.hyper_params)

    # optional keyword arguments passed to evaluate
    eval_kwargs = {}
    if 'eval_kwargs' in dir(algorithm):
        eval_kwargs = algorithm.eval_kwargs

    evaluate_model(args.INPUT_FILE, args.RDIR, args.RANDOM_STATE, args.ALG,
                   algorithm.est, algorithm.hyper_params, algorithm.complexity,
                   algorithm.model, test = args.TEST, 
                   target_noise=args.Y_NOISE, feature_noise=args.X_NOISE,
                   **eval_kwargs)

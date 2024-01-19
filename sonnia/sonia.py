#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Created on Wed Jan 30 12:06:58 2019
@author: Giulio Isacchini and Zachary Sethna
"""
from __future__ import print_function, division,absolute_import
import numpy as np
import os
os.environ['KMP_DUPLICATE_LIB_OK']='True'

from tensorflow.keras.models import Model,load_model
from tensorflow.keras.layers import Input,Dense,Lambda
from tensorflow.keras.optimizers import RMSprop
from tensorflow.keras.regularizers import l2, l1_l2
from tensorflow.keras.backend import clip as kclip
from tensorflow.keras.backend import sum as ksum
from tensorflow.keras.backend import log as klog
from tensorflow.keras.backend import exp as kexp
from tensorflow import cast,boolean_mask,Variable
from tensorflow import math
from tensorflow.keras.losses import BinaryCrossentropy
from tensorflow.keras.callbacks import ModelCheckpoint
from tensorflow import keras
import sonnia.sonnia
import olga.load_model as olga_load_model
import olga.generation_probability as pgen
import olga.sequence_generation as seq_gen
from copy import copy
from tqdm import tqdm
from sonnia.utils import compute_pgen_expand,compute_pgen_expand_novj,partial_joint_marginals,gene_to_num_str
import itertools
import multiprocessing as mp

#Set input = raw_input for python 2
try:
    import __builtin__
    input = getattr(__builtin__, 'raw_input')
except (ImportError, AttributeError):
    pass


class Sonia(object):
    """Class used to infer a Q selection model.
    Attributes
    ----------
    features : ndarray
        Array of feature lists. Each list contains individual subfeatures which
            all must be satisfied.
    features_dict : dict
        Dictionary keyed by tuples of the feature lists. Values are the index
        of the feature, i.e. self.features[self.features_dict[tuple(f)]] = f.
    constant_features : list
        List of feature strings to not update parameters during learning. These
            features are still used to compute energies (not currently used)
    data_seqs : list
        Data sequences used to infer selection model. Note, each 'sequence'
        is a list where the first element is the CDR3 sequence which is
        followed by any V or J genes.
    gen_seqs : list
        Sequences from generative distribution used to infer selection model.
        Note, each 'sequence' is a list where the first element is the CDR3
        sequence which is followed by any V or J genes.
    data_seq_features : list
        Lists of features that data_seqs project onto
    gen_seq_features : list
        Lists of features that gen_seqs project onto
    data_marginals : ndarray
        Array of the marginals of each feature over data_seqs
    gen_marginals : ndarray
        Array of the marginals of each feature over gen_seqs
    model_marginals : ndarray
        Array of the marginals of each feature over the model weighted gen_seqs
    L1_converge_history : list
        L1 distance between data_marginals and model_marginals at each
        iteration.
    chain_type : str
        Type of receptor. This specification is used to determine gene names
        and allow integrated OLGA sequence generation. Options: 'humanTRA',
        'humanTRB' (default), 'humanIGH', 'humanIGL', 'humanIGK' and 'mouseTRB'.
    l2_reg : float or None
        L2 regularization. If None (default) then no regularization.
    Methods
    ----------
    seq_feature_proj(feature, seq)
        Determines if a feature matches/is found in a sequence.
    find_seq_features(seq, features = None)
        Determines all model features of a sequence.
    compute_energy(seqs_features)
        Computes the energies of a list of seq_features according to the model.
    compute_marginals(self, features = None, seq_model_features = None, seqs = None, use_flat_distribution = False)
        Computes the marginals of features over a set of sequences.
    infer_selection(self, epochs = 20, batch_size=5000, initialize = True, seed = None)
        Infers model parameters (energies for each feature).
    update_model_structure(self,output_layer=[],input_layer=[],initialize=False)
        Sets keras model structure and compiles.
    update_model(self, add_data_seqs = [], add_gen_seqs = [], add_features = [], remove_features = [], add_constant_features = [], auto_update_marginals = False, auto_update_seq_features = False)
        Updates model by adding/removing model features or data/generated seqs.
        Marginals and seq_features can also be updated.
    add_generated_seqs(self, num_gen_seqs = 0, reset_gen_seqs = True)
        Generates synthetic sequences using OLGA and adds them to gen_seqs.
    plot_model_learning(self, save_name = None)
        Plots current marginal scatter plot as well as L1 convergence history.
    save_model(self, save_dir, attributes_to_save = None)
        Saves the model.
    load_model(self, load_dir)
        Loads a model.
    """

    def __init__(self, features = [], data_seqs = [], gen_seqs = [], chain_type = 'humanTRB',max_depth = 25, max_L = 30,
                 load_dir = None, l2_reg = 0., l1_reg=0.,min_energy_clip = -5, max_energy_clip = 10, 
                 seed = None, vj=False, custom_pgen_model=None, processes=None,objective='BCE',gamma=1,
                 include_indep_genes = True, include_joint_genes = False,joint_vjl=False,include_aminoacids=True):


        self.features = np.array(features)
        self.feature_dict = {tuple(f): i for i, f in enumerate(self.features)}
        self.data_seqs = []
        self.gen_seqs = []
        self.data_seq_features = []
        self.gen_seq_features = []
        self.data_marginals = np.zeros(len(features))
        self.gen_marginals = np.zeros(len(features))
        self.model_marginals = np.zeros(len(features))
        self.L1_converge_history = []
        self.l2_reg = l2_reg
        self.l1_reg = l1_reg
        self.min_energy_clip = min_energy_clip
        self.max_energy_clip = max_energy_clip
        self.likelihood_train=[]
        self.likelihood_test=[]
        self.objective=objective
        self.custom_pgen_model=custom_pgen_model
        self.include_indep_genes=include_indep_genes
        self.include_joint_genes=include_joint_genes
        self.joint_vjl=joint_vjl
        self.include_aminoacids=include_aminoacids
        self.max_depth=max_depth
        self.max_L = max_L
        if processes is None: self.processes = mp.cpu_count()
        self.gamma=gamma
        self.Z=1.
        self.amino_acids = 'ACDEFGHIKLMNPQRSTVWY'
        default_chain_types = { 'humanTRA': 'human_T_alpha', 'human_T_alpha': 'human_T_alpha', 
                                'humanTRB': 'human_T_beta', 'human_T_beta': 'human_T_beta', 
                                'humanIGH': 'human_B_heavy', 'human_B_heavy': 'human_B_heavy', 
                                'humanIGK': 'human_B_kappa', 'human_B_kappa': 'human_B_kappa', 
                                'humanIGL': 'human_B_lambda', 'human_B_lambda': 'human_B_lambda', 
                                'mouseTRB': 'mouse_T_beta', 'mouse_T_beta': 'mouse_T_beta',
                                'mouseTRA': 'mouse_T_alpha','mouse_T_alpha':'mouse_T_alpha'}
        if chain_type not in default_chain_types.keys():
            print('Unrecognized chain_type (not a default OLGA model). Please specify one of the following options: humanTRA, humanTRB, humanIGH, humanIGK, humanIGL, mouseTRB or mouseTRA.')
            return None
        self.chain_type = default_chain_types[chain_type]
        self.vj=vj
        if self.chain_type in ['human_T_alpha','human_B_kappa','human_B_lambda','mouse_T_alpha']: self.vj=True
        
        if load_dir is None:
            self.define_models(recompute_norm= not custom_pgen_model is None)
            self.update_model(add_data_seqs = data_seqs, add_gen_seqs = gen_seqs)
            self.add_features()
        else:
            self.load_model(load_dir = load_dir)
            if len(self.data_seqs) != 0: self.update_model(add_data_seqs = data_seqs)
            if len(self.gen_seqs) != 0: self.update_model(add_data_seqs = gen_seqs)

        if seed is not None:
            np.random.seed(seed = seed)

    def add_features(self):
        """Generates a list of feature_lsts for L/R pos model.

        Parameters
        ----------
        custom_pgen_model: string
            path to folder of custom olga model.

        """
        features = []
        if self.joint_vjl:
            features += [[v, j, 'l'+str(l)] for v in set([gene_to_num_str(genV[0],'V')for genV in self.genomic_data.genV]) for j in set([gene_to_num_str(genJ[0],'J') for genJ in self.genomic_data.genJ]) for l in range(1, self.max_L + 1)]
        else:
            features += [['l' + str(L)] for L in range(1, self.max_L + 1)]
        
        if self.include_aminoacids:
            for aa in self.amino_acids:
                features += [['a' + aa + str(L)] for L in range(self.max_depth)]
                features += [['a' + aa + str(L)] for L in range(-self.max_depth, 0)]  
                
        if self.include_indep_genes:
            features += [[v] for v in set([gene_to_num_str(genV[0],'V') for genV in self.genomic_data.genV])]
            features += [[j] for j in set([gene_to_num_str(genJ[0],'J') for genJ in self.genomic_data.genJ])]
        if self.include_joint_genes:
            features += [[v, j] for v in set([gene_to_num_str(genV[0],'V') for genV in self.genomic_data.genV]) for j in set([gene_to_num_str(genJ[0],'J') for genJ in self.genomic_data.genJ])]

        self.update_model(add_features=features)

    def find_seq_features(self, seq, features = None):
        """Finds which features match seq


        If no features are provided, the left/right indexing amino acid model
        features will be assumed.

        Parameters
        ----------
        seq : list
            CDR3 sequence and any associated genes
        features : ndarray
            Array of feature lists. Each list contains individual subfeatures which
            all must be satisfied.

        Returns
        -------
        seq_features : list
            Indices of features seq projects onto.

        """
        seq1,invseq=seq[0],seq[0][::-1]
        seq_feature_lsts = [['l' + str(len(seq1))]]
        for i in range(len(seq[0])):
            seq_feature_lsts += [['a' + seq1[i] + str(i)],['a' + invseq[i] + str(-1-i)]]
        v=gene_to_num_str(seq[1],'V')
        j=gene_to_num_str(seq[2],'J')
        seq_feature_lsts += [[v,j],[v],[j]]
        return list(set([self.feature_dict[tuple(f)] for f in seq_feature_lsts if tuple(f) in self.feature_dict]))

    def seq_feature_proj(self, feature, seq):
        """Checks if a sequence matches all subfeatures of the feature list
        Parameters
        ----------
        feature : list
            List of individual subfeatures the sequence must match
        seq : list
            CDR3 sequence and any associated genes
        Returns
        -------
        bool
            True if seq matches feature else False.
        """

        try:
            for f in feature:
                if f[0] == 'a': #Amino acid subfeature
                    if len(f) == 2:
                        if f[1] not in seq[0]:
                            return False
                    elif seq[0][int(f[2:])] != f[1]:
                        return False
                elif f[0] == 'v' or f[0] == 'j': #Gene subfeature
                        if not any([[int(x) for x in f[1:].split('-')] == [int(y) for y in gene.lower().split(f[0])[-1].split('-')] for gene in seq[1:] if f[0] in gene.lower()]):
                            if not any([[int(x) for x in f[1:].split('-')] == [int(gene.lower().split(f[0])[-1].split('-')[0])] for gene in seq[1:] if f[0] in gene.lower()]): #Checks for gene-family match if specified as such
                                return False
                elif f[0] == 'l': #CDR3 length subfeature
                    if len(seq[0]) != int(f[1:]):
                        return False
        except: #All ValueErrors and IndexErrors return False
            return False

        return True

    def compute_energy(self,seqs_features):
        """Computes the energy of a list of sequences according to the model.
        Parameters
        ----------
        seqs_features : list
            list of encoded sequences into sonnia features.
        Returns
        -------
        E : float
            Energies of seqs according to the model.
        """
        seqs_features_enc=self._encode_data(seqs_features)
        return self.model.predict(seqs_features_enc,verbose=0)[:, 0]

    def _encode_data(self,seq_features):
        """Turns seq_features into expanded numpy array"""

        if len(seq_features[0])==0: seq_features=[seq_features]
        length_input=len(self.features)
        data_enc = np.zeros((len(seq_features), length_input), dtype=np.int8)
        for i in range(len(data_enc)): data_enc[i][seq_features[i]] = 1
        return data_enc


    def compute_marginals(self, features = None, seq_model_features = None, seqs = None, use_flat_distribution = False, output_dict = False):
        """Computes the marginals of each feature over sequences.
        Computes marginals either with a flat distribution over the sequences
        or weighted by the model energies. Note, finding the features of each
        sequence takes time and should be avoided if it has already been done.
        If computing marginals of model features use the default setting to
        prevent searching for the model features a second time. Similarly, if
        seq_model_features has already been determined use this to avoid
        recalculating it.
        Parameters
        ----------
        features : list or None
            List of features. This does not need to match the model
            features. If None (default) the model features will be used.
        seq_features_all : list
            Indices of model features seqs project onto.
        seqs : list
            List of sequences to compute the feature marginals over. Note, each
            'sequence' is a list where the first element is the CDR3 sequence
            which is followed by any V or J genes.
        use_flat_distribution : bool
            Marginals will be computed using a flat distribution (each seq is
            weighted as 1) if True. If False, the marginals are computed using
            model weights (each sequence is weighted as exp(-E) = Q). Default
            is False.
        Returns
        -------
        marginals : ndarray or dict
            Marginals of model features over seqs.
        """
        if seq_model_features is None:  # if model features are not given
            if seqs is not None:  # if sequences are given, get model features from them
                seq_model_features = [self.find_seq_features(seq) for seq in seqs]
            else:   # if no sequences are given, sequences features is empty
                seq_model_features = []

        if len(seq_model_features) == 0:  # if no model features are given, return empty array
            return np.array([])

        if features is not None and seqs is not None:  # if a different set of features for the marginals is given
            seq_compute_features = [self.find_seq_features(seq, features = features) for seq in seqs]
        else:  # if no features or no sequences are provided, compute marginals using model features
            seq_compute_features = seq_model_features
            features = self.features

        marginals = np.zeros(len(features))
        if len(seq_model_features) != 0:
            Z = 0.
            if not use_flat_distribution:
                energies = self.compute_energy(seq_model_features)
                Qs= np.exp(-energies)
                for seq_features,Q in zip(seq_compute_features,Qs):
                    marginals[seq_features] += Q
                    Z += Q
            else:
                for seq_features in seq_compute_features:
                    marginals[seq_features] += 1.
                    Z += 1.

            marginals = marginals / Z
        return marginals

    def infer_selection(self, epochs = 10, batch_size=5000, initialize = True, seed = None,validation_split=0.2, monitor=False,verbose=0,set_gauge=True):
        """Infer model parameters, i.e. energies for each model feature.
        Parameters
        ----------
        epochs : int
            Maximum number of learning epochs
        intialize : bool
            Resets data shuffle
        batch_size : int
            Size of the batches in the inference
        seed : int
            Sets random seed
        Attributes set
        --------------
        model : keras model
            Parameters of the model
        model_marginals : array
            Marginals over the generated sequences, reweighted by the model.
        """

        if seed is not None:
            np.random.seed(seed = seed)
        if initialize:
            # prepare data
            self.X = np.array(self.data_seq_features+self.gen_seq_features, dtype=object)
            self.Y = np.concatenate([np.zeros(len(self.data_seq_features)), np.ones(len(self.gen_seq_features))])

            shuffle = np.random.permutation(len(self.X)) # shuffle
            self.X=self.X[shuffle]
            self.Y=self.Y[shuffle]

        
        callbacks=[]
        self.learning_history = self.model.fit(self._encode_data(self.X), self.Y, epochs=epochs, batch_size=batch_size,
                                          validation_split=validation_split, verbose=verbose, callbacks=callbacks)
        self.likelihood_train=-np.array(self.learning_history.history['_likelihood'])*1.44
        self.likelihood_test=-np.array(self.learning_history.history['val__likelihood'])*1.44
        
        # set Z    
        self.energies_gen=self.compute_energy(self.gen_seq_features)
        self.Z=np.mean(np.exp(-self.energies_gen))
        if set_gauge: self.set_gauge()
        self.update_model(auto_update_marginals=True)

    def set_gauge(self):
        """
        sets gauge such as sum(q)_i =1 at each position of CDR3 (left and right).
        """

        model_energy_parameters = self.model.get_weights()[0].flatten()

        Gs_plus=[]
        for i in list(range(self.max_depth)):
            norm_p=sum([self.gen_marginals[self.feature_dict[( 'a' + aa + str(i),)]] for aa in self.amino_acids])
            if norm_p==0:            
                G=1
            else:
                G = sum([self.gen_marginals[self.feature_dict[( 'a' + aa + str(i),)]] /norm_p * 
                          np.exp(-model_energy_parameters[self.feature_dict[( 'a' + aa + str(i),)]])
                          for aa in self.amino_acids])
            Gs_plus.append(G)
            for aa in self.amino_acids: model_energy_parameters[self.feature_dict[( 'a' + aa + str(i),)]] += np.log(G)
        Gs_minus=[]     
        for i in list(range(-self.max_depth,0)):
            norm_p=sum([self.gen_marginals[self.feature_dict[( 'a' + aa + str(i),)]] for aa in self.amino_acids])
            if norm_p==0:            
                G=1
            else:
                G = sum([self.gen_marginals[self.feature_dict[( 'a' + aa + str(i),)]] /norm_p * 
                          np.exp(-model_energy_parameters[self.feature_dict[( 'a' + aa + str(i),)]])
                          for aa in self.amino_acids])
            Gs_minus.append(G)
            for aa in self.amino_acids: model_energy_parameters[self.feature_dict[( 'a' + aa + str(i),)]] += np.log(G)
        for i in range(1,self.max_L+1):
            for j in list(range(i,self.max_depth)):
                model_energy_parameters[self.feature_dict[( 'l' + str(i),)]] += np.log(Gs_plus[j])
            for j in list(range(-self.max_depth,-i)):
                model_energy_parameters[self.feature_dict[( 'l' + str(i),)]] += np.log(Gs_minus[j])
        delta_Z=np.sum([np.log(g) for g in Gs_plus])+np.sum([np.log(g) for g in Gs_minus])

        self.min_energy_clip=self.min_energy_clip+delta_Z
        self.max_energy_clip=self.max_energy_clip+delta_Z
        self.Z=self.Z*np.exp(-delta_Z)
        self.update_model_structure(initialize=True)
        self.model.set_weights([np.array([model_energy_parameters]).T])

    def update_model_structure(self,output_layer=[],input_layer=[],initialize=False):
        """ Defines the model structure and compiles it.
        Parameters
        ----------
        structure : Sequential Model Keras
            structure of the model
        initialize: bool
            if True, it initializes to linear model, otherwise it updates to new structure
        """
        length_input=np.max([len(self.features),1])
        min_clip=copy(self.min_energy_clip)
        max_clip=copy(self.max_energy_clip)
        l2_reg=copy(self.l2_reg)
        l1_reg=copy(self.l1_reg)

        if initialize:
            input_layer = Input(shape=(length_input,))
            output_layer = Dense(1,use_bias=False,activation='linear',kernel_regularizer=l1_l2(l2=l2_reg,l1=l1_reg))(input_layer) #normal glm model

        # Define model
        clipped_out=Lambda(lambda x: kclip(x,min_clip,max_clip))(output_layer)
        self.model = Model(inputs=input_layer, outputs=clipped_out)

        self.optimizer = RMSprop()
        if self.objective=='BCE':
            self.model.compile(optimizer=self.optimizer, loss=BinaryCrossentropy(from_logits=True),metrics=[self._likelihood, BinaryCrossentropy(from_logits=True,name='binary_crossentropy')])
        else:
            self.model.compile(optimizer=self.optimizer, loss=self._loss, 
                               metrics=[self._likelihood, BinaryCrossentropy(from_logits=True,name='binary_crossentropy')])
        self.model_params = self.model.get_weights()        
        return True

    def _loss(self, y_true, y_pred):
        
        """Loss function for keras training. 
            We assume a model of the form P(x)=exp(-E(x))P_0(x)/Z.
            We minimize the neg-loglikelihood: <-logP> = log(Z) - <-E>.
            Normalization of P gives Z=<exp(-E)>_{P_0}.
            We fix the gauge by adding the constraint (Z-1)**2 to the likelihood.
        """
        y=cast(y_true,dtype='bool')
        data= math.reduce_mean(boolean_mask(y_pred,math.logical_not(y)))
        gen= math.reduce_logsumexp(-boolean_mask(y_pred,y))-klog(ksum(y_true))
        return gen+data+self.gamma*gen*gen

    def _likelihood(self, y_true, y_pred):
        
        """
        
        This is the "I" loss in the arxiv paper with added regularization
        
        """
        y=cast(y_true,dtype='bool')
        data= math.reduce_mean(boolean_mask(y_pred,math.logical_not(y)))
        gen= math.reduce_logsumexp(-boolean_mask(y_pred,y))-klog(ksum(y_true))
        return gen+data

    def update_model(self, add_data_seqs = [], add_gen_seqs = [], add_features = [], remove_features = [], add_constant_features = [], auto_update_marginals = False, auto_update_seq_features = False):
        """Updates the model attributes
        This method is used to add/remove model features or data/generated
        sequences. These changes will be propagated through the class to update
        any other attributes that need to match (e.g. the marginals or
        seq_features).
        Parameters
        ----------
        add_data_seqs : list
            List of CDR3 sequences to add to data_seq pool.
        add_gen_seqs : list
            List of CDR3 sequences to add to data_seq pool.
        add_gen_seqs : list
            List of CDR3 sequences to add to data_seq pool.
        add_features : list
            List of feature lists to add to self.features
        remove_featurese : list
            List of feature lists and/or indices to remove from self.features
        add_constant_features : list
            List of feature lists to add to constant features. (Not currently used)
        auto_update_marginals : bool
            Specifies to update marginals.
        auto_update_seq_features : bool
            Specifies to update seq features.
        Attributes set
        --------------
        features : list
            List of model features
        data_seq_features : list
            Features data_seqs have been projected onto.
        gen_seq_features : list
            Features gen_seqs have been projected onto.
        data_marginals : ndarray
            Marginals over the data sequences for each model feature.
        gen_marginals : ndarray
            Marginals over the generated sequences for each model feature.
        model_marginals : ndarray
            Marginals over the generated sequences, reweighted by the model,
            for each model feature.
        """

        if len(remove_features) > 0:
            indices_to_keep = [i for i, feature_lst in enumerate(self.features) if feature_lst not in remove_features and i not in remove_features]
            self.features = self.features[indices_to_keep]
            self.update_model_structure(initialize=True)
            self.feature_dict = {tuple(f): i for i, f in enumerate(self.features)}

        if len(add_features) > 0:
            if len(self.features) == 0:
                self.features = np.array(add_features, dtype=object)
            else:
                self.features = np.append(self.features, add_features, axis = 0)
            self.update_model_structure(initialize=True)
            self.feature_dict = {tuple(f): i for i, f in enumerate(self.features)}
        
        if len(add_data_seqs)>0:
            add_data_seqs=np.array([[seq,'',''] if type(seq)==str else seq for seq in add_data_seqs])
            if self.data_seqs==[]: self.data_seqs = add_data_seqs
            else: self.data_seqs = np.concatenate([self.data_seqs,add_data_seqs])

        if len(add_gen_seqs)>0:
            add_gen_seqs=np.array([[seq,'',''] if type(seq)==str else seq for seq in add_gen_seqs])
            if self.gen_seqs==[]: self.gen_seqs = add_gen_seqs
            else: self.gen_seqs = np.concatenate([self.gen_seqs,add_gen_seqs])

        if (len(add_data_seqs) + len(add_features) + len(remove_features) > 0 or auto_update_seq_features) and len(self.features)>0 and len(self.data_seqs)>0:
            print('Encode data.')
            self.data_seq_features = [self.find_seq_features(seq) for seq in tqdm(self.data_seqs)]

        if (len(add_data_seqs) + len(add_features) + len(remove_features) > 0 or auto_update_marginals > 0) and len(self.features)>0:
            self.data_marginals = self.compute_marginals(seq_model_features = self.data_seq_features, use_flat_distribution = True)

        if (len(add_gen_seqs) + len(add_features) + len(remove_features) > 0 or auto_update_seq_features) and len(self.features)>0 and len(self.gen_seqs)>0:
            print('Encode gen.')
            self.gen_seq_features = [self.find_seq_features(seq) for seq in tqdm(self.gen_seqs)]


        if (len(add_gen_seqs) + len(add_features) + len(remove_features) > 0 or auto_update_marginals) and len(self.features)>0:
            self.gen_marginals = self.compute_marginals(seq_model_features = self.gen_seq_features, use_flat_distribution = True)
            self.model_marginals = self.compute_marginals(seq_model_features = self.gen_seq_features)

    def add_generated_seqs(self, num_gen_seqs = 0, reset_gen_seqs = True, add_error=False, custom_error=None):
        """Generates MonteCarlo sequences for gen_seqs using OLGA.
        Only generates seqs from a V(D)J model. Requires the OLGA package
        (pip install olga).
        Parameters
        ----------
        num_gen_seqs : int or float
            Number of MonteCarlo sequences to generate and add to the specified
            sequence pool.
        custom_model_folder : str
            Path to a folder specifying a custom IGoR formatted model to be
            used as a generative model. Folder must contain 'model_params.txt'
            and 'model_marginals.txt'
        add_error: bool
            simualate sequencing error: default is false
        custom_error: int
            set custom error rate for sequencing error.
            Default is the one inferred by igor.
        Attributes set
        --------------
        gen_seqs : list
            MonteCarlo sequences drawn from a VDJ recomb model
        gen_seq_features : list
            Features gen_seqs have been projected onto.
        """
        seqs=self.generate_sequences_pre(num_gen_seqs,nucleotide=False)
        if reset_gen_seqs: self.gen_seqs = []
        self.update_model(add_gen_seqs = seqs)

    def save_model(self, save_dir, attributes_to_save = None,force=True):
        """Saves model parameters and sequences
        Parameters
        ----------
        save_dir : str
            Directory name to save model attributes to.
        attributes_to_save: list
            name of attributes to save
        """
        if attributes_to_save is None:
            attributes_to_save = ['model', 'log']

        if os.path.isdir(save_dir):
            if not force:
                if not input('The directory ' + save_dir + ' already exists. Overwrite existing model (y/n)? ').strip().lower() in ['y', 'yes']:
                    print('Exiting...')
                    return None
        else:
            os.mkdir(save_dir)

        if 'data_seqs' in attributes_to_save:
            with open(os.path.join(save_dir, 'data_seqs.tsv'), 'w') as data_seqs_file:
                data_seq_energies = self.compute_energy(self.data_seq_features)
                data_seqs_file.write('Sequence;Genes\tLog_10(Q)\tFeatures\n')
                data_seqs_file.write('\n'.join([';'.join(seq) + '\t' + str(-data_seq_energies[i]/np.log(10)) + '\t' + ';'.join([','.join(self.features[f]) for f in self.data_seq_features[i]]) for i, seq in enumerate(self.data_seqs)]))

        if 'gen_seqs' in attributes_to_save:
            with open(os.path.join(save_dir, 'gen_seqs.tsv'), 'w') as gen_seqs_file:
                gen_seq_energies = self.compute_energy(self.gen_seq_features)
                gen_seqs_file.write('Sequence;Genes\tLog_10(Q)\tFeatures\n')
                gen_seqs_file.write('\n'.join([';'.join(seq) + '\t' +  str(-gen_seq_energies[i]/np.log(10)) + '\t' + ';'.join([','.join(self.features[f]) for f in self.gen_seq_features[i]]) for i, seq in enumerate(self.gen_seqs)]))

        if 'log' in attributes_to_save: 
            with open(os.path.join(save_dir, 'log.txt'), 'w') as L1_file:
                L1_file.write('Z ='+str(self.Z)+'\n')
                L1_file.write('norm_productive ='+str(self.norm_productive)+'\n')
                L1_file.write('min_energy_clip ='+str(self.min_energy_clip)+'\n')
                L1_file.write('max_energy_clip ='+str(self.max_energy_clip)+'\n')
                L1_file.write('likelihood_train,likelihood_test\n')
                for i in range(len(self.likelihood_train)):
                    L1_file.write(str(self.likelihood_train[i])+','+str(self.likelihood_test[i])+'\n')

        if 'model' in attributes_to_save:
            with open(os.path.join(save_dir, 'features.tsv'), 'w') as feature_file:
                feature_file.write('Feature,marginal_data,marginal_model,marginal_gen\n')
                for i in range(len(self.features)):feature_file.write(';'.join(self.features[i])+','+str(self.data_marginals[i])+','+str(self.model_marginals[i])+','+str(self.gen_marginals[i])+'\n')
            self.model.save(os.path.join(save_dir, 'model.h5'))
        

        #save pgen model too.
        self._save_pgen_model(save_dir)
        return None

    def _save_pgen_model(self,save_dir):
        import shutil
        try:
            if self.custom_pgen_model is None: main_folder = os.path.join(os.path.dirname(sonnia.__file__), 'default_models', self.chain_type)
            else: main_folder=self.custom_pgen_model
        except:
            main_folder = os.path.join(os.path.dirname(sonnia.__file__), 'default_models', self.chain_type)
        shutil.copy2(os.path.join(main_folder,'model_params.txt'),save_dir)
        shutil.copy2(os.path.join(main_folder,'model_marginals.txt'),save_dir)
        shutil.copy2(os.path.join(main_folder,'V_gene_CDR3_anchors.csv'),save_dir)
        shutil.copy2(os.path.join(main_folder,'J_gene_CDR3_anchors.csv'),save_dir)

    def load_model(self, load_dir = None, verbose = True):
        """Loads model from directory.
        Parameters
        ----------
        load_dir : str
            Directory name to load model attributes from.
        """

        if load_dir is not None:
            if not os.path.isdir(load_dir):
                print('Directory for loading model does not exist (' + load_dir + ')')
                print('Exiting...')
                return None
            feature_file = os.path.join(load_dir, 'features.tsv')
            model_file = os.path.join(load_dir, 'model.h5')
            data_seq_file = os.path.join(load_dir, 'data_seqs.tsv')
            gen_seq_file = os.path.join(load_dir, 'gen_seqs.tsv')
            log_file = os.path.join(load_dir, 'log.txt')

        # start with pgen
        self._load_pgen_model(load_dir)

        if log_file is None:
            self.L1_converge_history = []
        elif os.path.isfile(log_file):
            with open(log_file, 'r') as L1_file:
                self.L1_converge_history=[]
                for i,line in enumerate(L1_file):
                    if i==0: self.Z=float(line.strip().split('=')[1])
                    elif i==1: self.norm_productive=float(line.strip().split('=')[1])
                    elif i==2: 
                        try: 
                            self.min_energy_clip=float(line.strip().split('=')[1])
                        except:
                            continue
                    elif i==3: 
                        try:
                            self.max_energy_clip=float(line.strip().split('=')[1])
                        except:
                            continue
                    elif len(line.strip())>0 and i>4: 
                        try:
                            self.likelihood_train.append(float(line.strip().split(',')[0]))
                            self.likelihood_test.append(float(line.strip().split(',')[1]))
                        except:
                            continue
                self.likelihood_train=np.array(self.likelihood_train)
                self.likelihood_test=np.array(self.likelihood_test)
        else:
            self.L1_converge_history = []
            self.likelihood_train= []
            self.likelihood_test= []
            if verbose: print('Cannot find log.txt  --  no norms and convergence loaded.')
                
        self._load_features_and_model(feature_file, model_file, verbose)

        if data_seq_file is None:
            pass
        elif os.path.isfile(data_seq_file):
            with open(data_seq_file, 'r') as data_seqs_file:
                self.data_seqs = []
                self.data_seq_features = []
                for line in data_seqs_file.read().strip().split('\n')[1:]:
                    split_line = line.split('\t')
                    self.data_seqs.append(split_line[0].split(';'))
                    self.data_seq_features.append([self.feature_dict[tuple(f.split(','))] for f in split_line[2].split(';') if tuple(f.split(',')) in self.feature_dict])
        elif verbose:
            print('Cannot find data_seqs.tsv  --  no data seqs loaded.')


        if gen_seq_file is None:
            pass
        elif os.path.isfile(gen_seq_file):
            with open(gen_seq_file, 'r') as gen_seqs_file:
                self.gen_seqs = []
                self.gen_seq_features = []
                for line in gen_seqs_file.read().strip().split('\n')[1:]:
                    split_line = line.split('\t')
                    self.gen_seqs.append(split_line[0].split(';'))
                    self.gen_seq_features.append([self.feature_dict[tuple(f.split(','))] for f in split_line[2].split(';') if tuple(f.split(',')) in self.feature_dict])
        elif verbose:
            print('Cannot find gen_seqs.tsv  --  no generated seqs loaded.')

        return None

    def _load_pgen_model(self,load_dir):
        self.custom_pgen_model=load_dir
        self.define_models(recompute_norm=False)

    def _load_features_and_model(self, feature_file, model_file, verbose = True):
        """Loads features and model.
        This is set as an internal function to allow daughter classes to load
        models from saved feature energies directly.
        """


        if feature_file is None and verbose:
            print('No feature file provided --  no features loaded.')
        elif os.path.isfile(feature_file):
            with open(feature_file, 'r') as features_file:
                lines = features_file.read().strip().split('\n') #skip header
                sonia_or_sonnia=lines[0].split(',')[1]
                if sonia_or_sonnia=='marginal_data':k=0
                else:k=1
                splitted=[l.split(',') for l in lines[1:]]
                self.features = np.array([l[0].split(';') for l in splitted],dtype=object)
                self.data_marginals=[float(l[1+k]) for l in splitted]
                self.model_marginals=[float(l[2+k]) for l in splitted]
                self.gen_marginals=[float(l[3+k]) for l in splitted]
                self.feature_dict = {tuple(f): i for i, f in enumerate(self.features)}

            if k==1:
                feature_energies = np.array([float(l[1]) for l in splitted]).reshape((len(self.features), 1))
                self.update_model_structure(initialize=True)
                self.model.set_weights([feature_energies])
            else:
                self.model = keras.models.load_model(model_file, custom_objects={'loss': self._loss,'likelihood': self._likelihood}, compile = False)
                self.optimizer = keras.optimizers.RMSprop()
                self.model.compile(optimizer=self.optimizer, loss=self._loss,metrics=[self._likelihood])

    def define_models(self,recompute_norm=True):
        '''
        load olga models.
        '''
        #Load generative model
        if self.custom_pgen_model is not None:
            main_folder = self.custom_pgen_model
        else:
            main_folder=os.path.join(os.path.dirname(sonnia.__file__),'default_models',self.chain_type)

        self.params_file_name = os.path.join(main_folder,'model_params.txt')
        self.marginals_file_name = os.path.join(main_folder,'model_marginals.txt')
        self.V_anchor_pos_file = os.path.join(main_folder,'V_gene_CDR3_anchors.csv')
        self.J_anchor_pos_file = os.path.join(main_folder,'J_gene_CDR3_anchors.csv')
        
        if not self.vj:
            self.genomic_data = olga_load_model.GenomicDataVDJ()
            self.genomic_data.load_igor_genomic_data(self.params_file_name, self.V_anchor_pos_file, self.J_anchor_pos_file)
            self.generative_model = olga_load_model.GenerativeModelVDJ()
            self.generative_model.load_and_process_igor_model(self.marginals_file_name)
            self.pgen_model = pgen.GenerationProbabilityVDJ(self.generative_model, self.genomic_data)
            self.seqgen_model = seq_gen.SequenceGenerationVDJ(self.generative_model, self.genomic_data)
        else:
            self.genomic_data = olga_load_model.GenomicDataVJ()
            self.genomic_data.load_igor_genomic_data(self.params_file_name, self.V_anchor_pos_file, self.J_anchor_pos_file)
            self.generative_model = olga_load_model.GenerativeModelVJ()
            self.generative_model.load_and_process_igor_model(self.marginals_file_name)
            self.pgen_model = pgen.GenerationProbabilityVJ(self.generative_model, self.genomic_data)
            self.seqgen_model = seq_gen.SequenceGenerationVJ(self.generative_model, self.genomic_data)

        with open(self.params_file_name,'r') as file:
            sep=0
            error_rate=''
            lines=file.read().splitlines()
            while len(error_rate)<1:
                error_rate=lines[-1+sep]
                sep-=1
            self.error_rate=float(error_rate)

        if recompute_norm:
            self.norm_productive= self.pgen_model.compute_regex_CDR3_template_pgen('CX{0,}') 
        else:
            norms={'human_T_beta':0.2442847269027897,'human_T_alpha':0.2847166577727317,'human_B_heavy': 0.1499265655936305, 
                'human_B_lambda':0.29489499727399304, 'human_B_kappa':0.29247125650320943, 'mouse_T_beta':0.2727148540013573,'mouse_T_alpha':0.321870924914448}
            self.norm_productive=norms[self.chain_type]

    def generate_sequences_pre(self, num_seqs = 1, nucleotide=False,custom_error=None,add_error=False):
            """Generates MonteCarlo sequences for gen_seqs using OLGA in parallel.
            Only generates seqs from a V(D)J model. Requires the OLGA package
            (pip install olga). If you add error_rate, only the aminoacid sequence is modified.
            Parameters
            ----------
            num_seqs : int or float
                Number of MonteCarlo sequences to generate and add to the specified
                sequence pool.
            
            Returns
            --------------
            seqs : list
                MonteCarlo sequences drawn from a VDJ recomb model
            """
            from sonia.utils import add_random_error
            from olga.utils import nt2aa

            if custom_error is None: error_rate=self.error_rate
            else: error_rate=custom_error
            #Generate sequences
            seqs_generated=[self.seqgen_model.gen_rnd_prod_CDR3(conserved_J_residues='ABCEDFGHIJKLMNOPQRSTUVWXYZ') for i in tqdm(range(int(num_seqs)))]
            seqs= [[seq[1], self.genomic_data.genV[seq[2]][0].split('*')[0], self.genomic_data.genJ[seq[3]][0].split('*')[0],seq[0]] for seq in seqs_generated]

            if add_error: 
                err_seqs = [add_random_error(seq[0],self.error_rate) for seq in seqs_generated]
                seqs= [[nt2aa(err_seqs[i]), seqs[i][1], seqs[i][2],err_seqs[i]] for i in range(len(seqs))]
            if nucleotide: return np.array(seqs)
            else: return np.array(seqs)[:,:-1] 

    def generate_sequences_post(self,num_seqs = 1,upper_bound=10,nucleotide=False):
        """Generates MonteCarlo sequences from Sonia through rejection sampling.
        Parameters
        ----------
        num_seqs : int or float
            Number of MonteCarlo sequences to generate and add to the specified
            sequence pool.
        upper_bound: int
            accept all above the threshold. Relates to the percentage of 
            sequences that pass selection.
        Returns
        --------------
        seqs : list
            MonteCarlo sequences drawn from a VDJ recomb model that pass selection.
        """
        seqs_out=[]
        while len(seqs_out)<num_seqs+1:

            # generate sequences from pre
            seqs=self.generate_sequences_pre(num_seqs = int(1.1*upper_bound*num_seqs),nucleotide=nucleotide)

            # compute features and energies 
            seq_features = [self.find_seq_features(seq) for seq in list(np.array(seqs))]
            energies = self.compute_energy(seq_features)

            #do rejection
            rejection_selection=self.rejection_sampling(upper_bound=upper_bound,energies=energies)

            if len(seqs_out)==0:seqs_out=np.array(seqs[rejection_selection])
            else: np.concatenate(seqs_out,seqs_post)
        return seqs_out[:num_seqs]

    def rejection_sampling(self,upper_bound=10,energies=None):

        ''' Returns acceptance from rejection sampling of a list of seqs.
        By default uses the generated sequences within the sonia model.
        
        Parameters
        ----------
        upper_bound : int or float
            accept all above the threshold. Relates to the percentage of 
            sequences that pass selection
        Returns
        -------
        rejection selection: array of bool
            acceptance of each sequence.
        
        '''

        if energies is None:  energies=self.energies_gen
        Q=np.exp(-energies)/self.Z
        random_samples=np.random.uniform(size=len(energies)) # sample from uniform distribution

        return random_samples < Q/float(upper_bound)

    def evaluate_seqs(self,seqs=[],include_genes=True):
        '''Returns selection factors, pgen and pposts of sequences.
        Parameters
        ----------
        seqs: list
            list of sequences to evaluate
        Returns
        -------
        Q: array
            selection factor Q (of Ppost=Q*Pgen) of the sequences
        pgens: array
            pgen of the sequences
        pposts: array
            ppost of the sequences
        '''

        seq_features = [self.find_seq_features(seq) for seq in seqs] #find seq features
        energies =self.compute_energy(seq_features) # compute energies
        Q= np.exp(-energies)/self.Z # compute Q
        pgens=self.compute_all_pgens(seqs,include_genes)/self.norm_productive # compute pgen
        pposts=pgens*Q # compute ppost

        return Q, pgens, pposts

    def evaluate_selection_factors(self,seqs=[]):
        '''Returns normalised selection factor Q (of Ppost=Q*Pgen) of list of sequences (faster than evaluate_seqs because it does not compute pgen and ppost)
        Parameters
        ----------
        seqs: list
            list of sequences to evaluate
        Returns
        -------
        Q: array
            selection factor Q (of Ppost=Q*Pgen) of the sequences
        '''

        seq_features = [self.find_seq_features(seq) for seq in seqs] #find seq features
        energies =self.compute_energy(seq_features) # compute energies

        return np.exp(-energies)/self.Z

    def joint_marginals(self, features = None, seq_model_features = None, seqs = None, use_flat_distribution = False):
        '''Returns joint marginals P(i,j) with i and j features of sonia (l3, aA6, etc..), index of features attribute is preserved.
           Matrix is upper-triangular.
        Parameters
        ----------
        features: list
            custom feature list 
        seq_model_features: list
            encoded sequences
        seqs: list
            seqs to encode.
        use_flat_distribution: bool
            for data and generated seqs is True, for model is False (weights with Q)
        Returns
        -------
        joint_marginals: array
            matrix (i,j) of joint marginals
        '''

        if seq_model_features is None:  # if model features are not given
            if seqs is not None:  # if sequences are given, get model features from them
                seq_model_features = [self.find_seq_features(seq) for seq in seqs]
            else:   # if no sequences are given, sequences features is empty
                seq_model_features = []

        if len(seq_model_features) == 0:  # if no model features are given, return empty array
            return np.array([])

        if features is not None and seqs is not None:  # if a different set of features for the marginals is given
            seq_compute_features = [self.find_seq_features(seq, features = features) for seq in seqs]
        else:  # if no features or no sequences are provided, compute marginals using model features
            seq_compute_features = seq_model_features
            features = self.features
        l=len(features)
        two_points_marginals=np.zeros((l,l)) 
        n=len(seq_model_features)
        procs = mp.cpu_count()
        sizeSegment = int(n/procs)
        
        if not use_flat_distribution:
            energies = self.compute_energy(seq_model_features)
            Qs= np.exp(-energies)
        else:
            Qs=np.ones(len(seq_compute_features))
            
        # Create size segments list
        jobs = []
        for i in range(0, procs-1):
            jobs.append([seq_model_features[i*sizeSegment:(i+1)*sizeSegment],Qs[i*sizeSegment:(i+1)*sizeSegment],np.zeros((l,l))])
        p=mp.Pool(procs)
        pool = p.map(partial_joint_marginals, jobs)
        p.close()
        Z=0
        two_points_marginals=0
        for m,z in pool:
            Z+=z
            two_points_marginals+=m
        return two_points_marginals/Z
    
    def joint_marginals_independent(self,marginals):
        '''Returns independent joint marginals P(i,j)=P(i)*P(j) with i and j features of sonia (l3, aA6, etc..), index of features attribute is preserved.
        Matrix is upper-triangular.
        Parameters
        ----------
        marginals: list
            marginals.
        Returns
        -------
        joint_marginals: array
            matrix (i,j) of joint marginals
        '''

        joint_marginals=np.zeros((len(marginals),len(marginals)))
        for i,j in itertools.combinations(np.arange(len(marginals)),2):
            if i>j:
                joint_marginals[i,j]=marginals[i]*marginals[j]
            else: 
                joint_marginals[j,i]=marginals[i]*marginals[j]
        return joint_marginals

    def compute_joint_marginals(self):
        '''Computes joint marginals for all.
        Attributes Set
        -------
        gen_marginals_two: array
            matrix (i,j) of joint marginals for pre-selection distribution
        data_marginals_two: array
            matrix (i,j) of joint marginals for data
        model_marginals_two: array
            matrix (i,j) of joint marginals for post-selection distribution
        gen_marginals_two_independent: array
            matrix (i,j) of independent joint marginals for pre-selection distribution
        data_marginals_two_independent: array
            matrix (i,j) of joint marginals for pre-selection distribution
        model_marginals_two_independent: array
            matrix (i,j) of joint marginals for pre-selection distribution
        '''

        self.gen_marginals_two = self.joint_marginals(seq_model_features = self.gen_seq_features, use_flat_distribution = True)
        self.data_marginals_two = self.joint_marginals(seq_model_features = self.data_seq_features, use_flat_distribution = True)
        self.model_marginals_two = self.joint_marginals(seq_model_features = self.gen_seq_features)
        self.gen_marginals_two_independent = self.joint_marginals_independent(self.gen_marginals)
        self.data_marginals_two_independent = self.joint_marginals_independent(self.data_marginals)
        self.model_marginals_two_independent = self.joint_marginals_independent(self.model_marginals)

    def compute_all_pgens(self,seqs,include_genes=True):
        '''Compute Pgen of sequences using OLGA in parallel
        Parameters
        ----------
        seqs: list
            list of sequences to evaluate.
        Returns
        -------
        pgens: array
            generation probabilities of the sequences.
        '''

        final_models = [self.pgen_model for i in range(len(seqs))]    # every process needs to access this vector.
        pool = mp.Pool(processes=self.processes)

        if include_genes:
            f=pool.map(compute_pgen_expand, zip(seqs,final_models))
            pool.close()
            return np.array(f)
        else:
            f=pool.map(compute_pgen_expand_novj, zip(seqs,final_models))
            pool.close()
            return np.array(f)
        
        
    def entropy(self,n=int(2e4),include_genes=True,remove_zeros=True):
        '''Compute Entropy of Model
        Returns
        -------
        entropy: float
            entropy of the model
        '''
        if len(self.gen_seq_features)> int(1e4):
            seq_features= self.gen_seq_features[:n]
            seqs= self.gen_seqs[:n]
        else:
            print('not enough generated sequences')
            return -1
        
        energies =self.compute_energy(seq_features) # compute energies
        self.gen_Q= np.exp(-energies)/self.Z # compute Q
        self.gen_pgen=self.compute_all_pgens(seqs,include_genes)/self.norm_productive # compute pgen
        sel=self.gen_pgen>0
        if np.sum(sel)!=len(sel): 
            print(len(sel)-np.sum(sel),'sequences have zero Pgen, we remove them in the evaluation of the entropy')
        self.gen_ppost=self.gen_pgen*self.gen_Q # compute ppost
        self._entropy=-np.mean(self.gen_Q[sel]*np.log2(self.gen_ppost[sel]))
        return self._entropy
    
    def dkl_post_gen(self,n=int(1e5)):
        '''Compute D_KL(P_post|P_gen)
        Returns
        -------
        dkl: float
            D_KL(P_post|P_gen)
        '''
        try: # energies gen exist
            Q=np.exp(-self.energies_gen)/self.Z
            self.dkl=np.mean(Q*np.log2(Q))
            return self.dkl
        except:
            if len(self.gen_seq_features)> int(1e4):
                seq_features= self.gen_seq_features[:n]
            else:
                print('not enough generated sequences')
                return -1
            energies =self.compute_energy(seq_features) # compute energies
            Q= np.exp(-energies)/self.Z # compute Q
            self.dkl=np.mean(Q*np.log2(Q))
        return self.dkl
    
    def load_default_model(self,chain_type=None):
        if chain_type is None:
            chain_type=self.chain_type
        load_dir=os.path.join(os.path.dirname(sonnia.__file__), 'default_models', chain_type)
        if chain_type in ['human_T_alpha','human_B_kappa','human_B_lambda','mouse_T_alpha']: self.vj=True
        self.load_model(load_dir = load_dir)
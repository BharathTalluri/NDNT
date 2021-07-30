### NDNtorch.py
# this defines NDNclass
import numpy as np
import torch
from torch import nn

# Imports from my code
from NDNLosses import *
#from NDNencoders import Encoder
#from NDNutils import get_trainer
from NDNlayer import *
from FFnetworks import *
from NDNutils import create_optimizer_params

FFnets = {
    'normal': FFnetwork,
    'readout': Readout
}

class NDN(nn.Module):

    def __init__(self,
        ffnet_list = None,
        loss_type = 'poisson',
        ffnet_out = [-8], # Default output is last
        optimizer_params = None,
        model_name='NDN_model',
        data_dir='./checkpoints'):

        print('first', ffnet_out)
        super().__init__()
        self.model_name = model_name
        self.data_dir = data_dir
        print('first', ffnet_out)
        # Assign optimizer params
        if optimizer_params is None:
            optimizer_params = create_optimizer_params()

        # Assign loss function (from list)
        if isinstance(loss_type, str):
            self.loss_type = loss_type
            if loss_type == 'poisson':
                loss_func = PoissonLoss_datafilter()  # defined below, but could be in own Losses.py
            elif loss_type == 'gaussian':
                print('Gaussian loss_func not implemented yet.')
                loss_func = None
            else:
                print('Invalid loss function.')
                loss_func = None
        else: # assume passed in loss function directly
            self.loss_type = 'custom'
            loss_func = loss_type

        # Has both reduced and non-reduced for other eval functions
        self.loss_module = loss_func

        # Assemble FFnetworks and put into encoder (f passed in network-list)
        if type(ffnet_list) is list:
            network_list = self.assemble_ffnetworks(ffnet_list)
        else:  # assume passed in external module
            # can check type here if we want
            network_list = [ffnet_list]  # list of single network with forward

        # Check and record output of network
        if not isinstance(ffnet_out, list):
            ffnet_out = [ffnet_out]
        print(ffnet_out)        
        for nn in range(len(ffnet_out)):
            if ffnet_out[nn] == -1:
                ffnet_out[nn] = len(network_list)-1
        print(ffnet_out)
        assert network_list is not None, 'Missing encoder' 
        assert loss_func is not None, 'Missing loss_function' 
        
        # Assemble ffnetworks
        self.networks = network_list
        self.ffnet_out = ffnet_out
        
        self.loss = loss_func
        self.val_loss = loss_func
   
        self.opt_params = optimizer_params
        self.name = model_name
    # END NDN.__init__

    def assemble_ffnetworks(self, ffnet_list):
        """This function takes a list of ffnetworks and puts them together 
        in order. This has to do two steps for each ffnetwork: 
        1. Plug in the inputs to each ffnetwork as specified
        2. Builds the ff-network with the input
        This returns the a 'network', which is (currently) a [lightning] module with a 
        'forward' and 'reg_loss' function specified.

        When multiple ffnet inputs are concatenated, it will always happen in the first
        (filter) dimension, so all other dimensions must match
        """
        assert type(ffnet_list) is list, "Yo ffnet_list is screwy."
        
        num_networks = len(ffnet_list)
        # Make list of lightning modules
        networks = nn.ModuleList()

        for mm in range(num_networks):

            # Dete rmine network input
            if ffnet_list[mm]['ffnet_n'] is None:
                # then external input (assume from one source)
                input_dims = ffnet_list[mm]['input_dims']
                assert input_dims is not None, "FFnet%d: External input dims must be specified"%mm
            else: 
                nets_in = ffnet_list[mm]['ffnet_n']
                num_input_networks = len(nets_in)

                # Concatenate input dimensions into first filter dims and make sure valid
                input_dim_list, valid_concat = [], True
                for ii in range(num_input_networks):
                    assert nets_in[ii] < mm, "FFnet%d (%d): input networks must come earlier"%(mm, ii)
                    
                    # How reads input networks depends on what type of network this is
                    #if ffnet_list[mm]['ffnet_type'] == 'normal':
                        # this means that just takes output of last layer of input network
                    input_dim_list.append(networks[nets_in[ii]].layers[-1].output_dims)
                    #else:
                    #    print('currently no dim combo rules for non-normal ffnetworks')
                    if ii == 0:
                        num_cat_filters = input_dim_list[0][0]
                    else:
                        if input_dim_list[ii][1:] == input_dim_list[0][1:]:
                            num_cat_filters += input_dim_list[ii][0]
                        else:
                            valid_concat = False
                            print("FFnet%d: invalid concatenation %d:"%(mm,ii), input_dim_list[ii][1:], input_dim_list[0][1:] )
                assert valid_concat, "Dim concat error. Exiting."
                input_dims = [num_cat_filters] + input_dim_list[0][1:]

            ffnet_list[mm]['input_dims'] = input_dims
            net_type = ffnet_list[mm]['ffnet_type']

            # Create corresponding FFnetwork
            networks.append( FFnets[net_type](ffnet_list[mm]) )

        return networks
    # END assemble_ffnetworks

    def compute_network_outputs( self, Xs):
        if type(Xs) is not list:
            Xs = [Xs]

        net_ins, net_outs = [], []
        for nn in range(len(self.networks)):
            if self.networks[nn].ffnets_in is None:
                # then getting external input
                #net_ins.append( Xs[self.networks[nn].xstim_n] )
                net_outs.append( self.networks[nn]( Xs[self.networks[nn].xstim_n] ) )
            else:
                # Concatenate the previous relevant network outputs
                in_nets = self.networks[nn].ffnets_in
                input_cat = net_outs[in_nets[0]]
                for mm in range(1, len(in_nets)):
                    input_cat = torch.cat( (input_cat, net_outs[in_nets[mm]]), 1 )

                #net_ins.append( input_cat )
                net_outs.append( self.networks[nn](input_cat) ) 
        return net_ins, net_outs
    # END compute_network_outputs

    def forward(self, Xs):
        """This applies the forwards of each network in sequential order.
        The tricky thing is concatenating multiple-input dimensions together correctly.
        Note that the external inputs is actually in principle a list of inputs"""

        net_ins, net_outs = self.compute_network_outputs( Xs )

        # For now assume its just one output, given by the first value of self.ffnet_out
        return net_outs[self.ffnet_out[0]]
    # END Encoder.forward

    def training_step(self, batch, batch_idx=None):  # batch_indx not used, right?
        x = batch['stim'] # TODO: this will have to handle the multiple Xstims in the future
        y = batch['robs']
        dfs = batch['dfs']

        #if self.readout.shifter is not None and batch['eyepos'] is not None and self.readout.shifter:
        #    y_hat = self(x, shifter=batch['eyepos'])
        #else:
        y_hat = self(x)

        loss = self.loss(y_hat, y, dfs)

        regularizers = self.compute_reg_loss()

        return {'loss': loss + regularizers, 'train_loss': loss, 'reg_loss': regularizers}
    # END Encoder.training_step

    def validation_step(self, batch, batch_idx=None):
        x = batch['stim']
        y = batch['robs']
        dfs = batch['dfs']

        y_hat = self(x)
        loss = self.val_loss(y_hat, y, dfs)
        
        reg_loss = self.compute_reg_loss()
        
        return {'loss': loss, 'val_loss': loss, 'reg_loss': reg_loss}
    
    def compute_reg_loss(self):
        rloss = 0
        for network in self.networks:
            rloss += network.compute_reg_loss()
        return rloss

    def out( self, x):
        return self(x)
    
    def get_trainer(self, dataset,
        version=None,
        save_dir='./checkpoints',
        name='jnkname',
        opt_params = None):
        """
            Returns a trainer and object splits the training set into "train" and "valid"
        """
        from torch.utils.data import DataLoader, random_split
        from trainers import Trainer, EarlyStopping
        from pathlib import Path
    
        save_dir = Path(save_dir)
        batchsize = opt_params['batch_size']
        model = self
        n_val = np.floor(len(dataset)/5).astype(int)
        n_train = (len(dataset)-n_val).astype(int)

        gd_train, gd_val = random_split(dataset, lengths=[n_train, n_val])

        # build dataloaders
        train_dl = DataLoader(gd_train, batch_size=batchsize, num_workers=opt_params['num_workers'])
        valid_dl = DataLoader(gd_val, batch_size=batchsize, num_workers=opt_params['num_workers'])

        # get optimizer: In theory this probably shouldn't happen here because it needs to know the model
        # but this was the easiest insertion point I could find for now
        if opt_params['optimizer']=='AdamW':
            optimizer = torch.optim.AdamW(model.parameters(),
                    lr=opt_params['learning_rate'],
                    betas=opt_params['betas'],
                    weight_decay=opt_params['weight_decay'],
                    amsgrad=opt_params['amsgrad'])

        elif opt_params['optimizer']=='Adam':
            optimizer = torch.optim.Adam(model.parameters(),
                    lr=opt_params['learning_rate'],
                    betas=opt_params['betas'])

        elif opt_params['optimizer']=='LBFGS':
            from LBFGS import LBFGS
            optimizer = LBFGS(model.parameters(), lr=opt_params['learning_rate'], history_size=10, line_search='Wolfe', debug=False)

        elif opt_params['optimizer']=='FullBatchLBFGS':
            from LBFGS import FullBatchLBFGS
            optimizer = FullBatchLBFGS(model.parameters(), lr=opt_params['learning_rate'], history_size=10, line_search='Wolfe', debug=False)

        else:
            raise ValueError('optimizer [%s] not supported' %opt_params['optimizer'])
            

        if opt_params['early_stopping']:
            if isinstance(opt_params['early_stopping'], EarlyStopping):
                earlystopping = opt_params['early_stopping']
            elif isinstance(opt_params['early_stopping'], dict):
                earlystopping = EarlyStopping(patience=opt_params['early_stopping']['patience'],delta=opt_params['early_stopping']['delta'])
            else:
                earlystopping = EarlyStopping(patience=opt_params['early_stopping_patience'],delta=0.0)
        else:
            earlystopping = None

        trainer = Trainer(model, optimizer, early_stopping=earlystopping,
                dirpath=save_dir,
                version=version) # TODO: how do we want to handle name? Variable name is currently unused

        return trainer, train_dl, valid_dl
    
    def fit( self, dataset, version=None, save_dir=None, name=None, seed=None):
        '''
        This is the main training loop.
        Steps:
            1. Get a trainer and dataloaders
            2. Prepare regularizers
            3. Run the main fit loop from the trainer, checkpoint, and save model
        '''
        import time

        if save_dir is None:
            save_dir = self.data_dir
        
        if name is None:
            name = self.name

        # get trainer 
        trainer, train_dl, valid_dl = self.get_trainer(
            dataset,
            version=version,
            save_dir=save_dir, name = name,
            opt_params = self.opt_params)

        # Make reg modules
        for network in self.networks:
            network.prepare_regularization()

        t0 = time.time()
        trainer.fit( self, train_dl, valid_dl, seed=seed)
        t1 = time.time()

        print('  Fit complete:', t1-t0, 'sec elapsed')

    # END NDN.train
        
    def eval_models(self, sample, bits=False, null_adjusted=True):
        '''
        get null-adjusted log likelihood
        bits=True will return in units of bits/spike
        '''
        m0 = self.cpu()
        yhat = m0(sample['stim'])
        y = sample['robs']
        dfs = sample['dfs']

        if self.loss_type == 'poisson':
            #loss = nn.PoissonNLLLoss(log_input=False, reduction='none')
            loss = self.loss_module.lossNR
        else:
            print("This loss-type is not supported for eval_models.")
            loss = None

        LLraw = torch.sum(
            torch.multiply( 
                dfs, 
                loss(yhat, y)),
            axis=0).detach().cpu().numpy()
        obscnt = torch.sum(
            torch.multiply(dfs, y), axis=0).detach().cpu().numpy()
        
        Ts = np.maximum(torch.sum(dfs, axis=0).detach().cpu().numpy(), 1)

        LLneuron = LLraw / np.maximum(obscnt,1) # note making positive

        if null_adjusted:
            predcnt = torch.sum(
                torch.multiply(dfs, yhat), axis=0).detach().cpu().numpy()
            rbar = np.divide(predcnt, Ts)
            LLnulls = np.log(rbar)-np.divide(predcnt, np.maximum(obscnt,1))
            LLneuron = -LLneuron - LLnulls 

        if bits:
            LLneuron/=np.log(2)
        return LLneuron

    def get_weights(self, ffnet_target=0, layer_target=0, to_reshape=True):
        return self.networks[ffnet_target].layers[layer_target].get_weights(to_reshape)

    def get_readout_positions(self):
        return self.network.mu.detach().cpu().numpy().squeeze()


    def plot_filters(self, cmaps):
        import matplotlib.pyplot as plt
        self.network.plot_filters(cmaps=cmaps)
        plt.show()

    def save_model(self, filename=None, alt_dirname=None):
        """Models will be saved using dill/pickle in the directory above the version
        directories, which happen to be under the model-name itself. This assumes the
        current save-directory (notebook specific) and the model name"""

        import dill
        if alt_dirname is None:
            fn = './checkpoints/'
        else:
            fn = alt_dirname
            if alt_dirname != '/':
                fn += '/'
        if filename is None:
            fn += self.name + '.pkl'
        else :
            fn += filename
        print( '  Saving model at', fn)

        with open(fn, 'wb') as f:
            dill.dump(self, f)

    def get_null_adjusted_ll(self, sample, bits=False):
        '''
        get null-adjusted log likelihood
        bits=True will return in units of bits/spike
        '''
        m0 = self.cpu()
        if self.loss_type == 'poisson':
            #loss = nn.PoissonNLLLoss(log_input=False, reduction='none')
            loss = self.loss_module.lossNR
        else:
            print('Whatever loss function you want is not yet here.')
        
        lnull = -loss(torch.ones(sample['robs'].shape)*sample['robs'].mean(axis=0), sample['robs']).detach().cpu().numpy().sum(axis=0)
        #yhat = m0(sample['stim'], shifter=sample['eyepos'])
        yhat = m0(sample['stim'])
        llneuron = -loss(yhat,sample['robs']).detach().cpu().numpy().sum(axis=0)
        rbar = sample['robs'].sum(axis=0).numpy()
        ll = (llneuron - lnull)/rbar
        if bits:
            ll/=np.log(2)
        return ll

    @classmethod
    def load_model(cls, checkpoint_path=None, model_name=None, version=None):
        '''
            Load a model from disk.
            Arguments:


        '''
        from NDNutils import get_fit_versions

        assert checkpoint_path is not None, "Need to provide a checkpoint_path"
        assert model_name is not None, "Need to provide a model_name"

        out = get_fit_versions(checkpoint_path, model_name)
        if version is None:
            version = out['version_num'][np.argmax(np.asarray(out['val_loss']))]
            print("No version requested. Using (best) version (v=%d)" %version)

        assert version in out['version_num'], "Version %d not found in %s" %(version, checkpoint_path)
        ver_ix = np.where(version==np.asarray(out['version_num']))[0][0]
        # Load the model
        model = torch.load(out['model_file'][ver_ix])
        
        return model

        # import os
        # import dill
        # if alt_dirname is None:
        #     fn = './checkpoints/'
        # else:
        #     fn = alt_dirname
        #     if alt_dirname != '/':
        #         fn += '/'
        # if filename is None:
        #     assert model_name is not None, 'Need model_name or filename.'
        #     fn += model_name + '.pkl'
        # else :
        #     fn += filename

        # if not os.path.isfile(fn):
        #     raise ValueError(str('%s is not a valid filename' %fn))

        # print( 'Loading model:', fn)
        # with open(fn, 'rb') as f:
        #     model = dill.load(f)
        # model.encoder = None
        # if version is not None:
        #     from pathlib import Path
        #     assert filename is None, 'Must recover version from checkpoint dir.'
        #     # Then load checkpointed encoder on top of model
        #     chkpntdir = fn[:-4] + '/version_' + str(version) + '/'
        #     chkpath = Path(chkpntdir) / 'checkpoints'
        #     ckpt_files = list(chkpath.glob('*.ckpt'))
        #     model.encoder = Encoder.load_from_checkpoint(str(ckpt_files[0]))
        #     nn.utils.remove_weight_norm(model.encoder.core.features.layer0.conv)
        #     print( '-> Updated with', str(ckpt_files[0]))

        # return model

"""

ModelFit.py

Author: Jordan Mirocha
Affiliation: University of Colorado at Boulder
Created on: Mon Apr 28 11:19:03 MDT 2014

Description: For analysis of MCMC fitting.

"""

import pickle
import shutil
import numpy as np
import matplotlib as mpl
import matplotlib.pyplot as pl
from ..util import ProgressBar
import matplotlib._cntr as cntr
from ..physics import Cosmology
from .MultiPlot import MultiPanel
import re, os, string, time, glob
from .BlobFactory import BlobFactory
from matplotlib.colors import Normalize
from matplotlib.patches import Rectangle
from ..phenom.DustCorrection import DustCorrection
from .MultiPhaseMedium import MultiPhaseMedium as aG21
from ..physics.Constants import nu_0_mhz, erg_per_ev, h_p
from ..util import labels as default_labels
from ..util.Pickling import read_pickle_file, write_pickle_file
import matplotlib.patches as patches
from ..util.Aesthetics import Labeler
from ..util.PrintInfo import print_model_set
from .DerivedQuantities import DerivedQuantities as DQ
from ..util.ParameterFile import count_populations, par_info
from matplotlib.collections import PatchCollection, LineCollection
from ..util.SetDefaultParameterValues import SetAllDefaults, TanhParameters
from ..util.Stats import Gauss1D, GaussND, error_2D, _error_2D_crude, \
    rebin, correlation_matrix
from ..util.ReadData import concatenate, read_pickled_chain,\
    read_pickled_logL, fcoll_gjah_to_ares, tanh_gjah_to_ares
try:
    # this runs with no issues in python 2 but raises error in python 3
    basestring
except:
    # this try/except allows for python 2/3 compatible string type checking
    basestring = str

try:
    from scipy.spatial import Delaunay
except ImportError:
    pass

try:
    import shapely.geometry as geometry
    from shapely.ops import cascaded_union, polygonize, unary_union
    have_shapely = True
except (ImportError, OSError):
    have_shapely = False
    
try:
    from descartes import PolygonPatch
    have_descartes = True
except ImportError:
    have_descartes = False    

try:
    import h5py
    have_h5py = True
except ImportError:
    have_h5py = False
    
try:
    from mpi4py import MPI
    rank = MPI.COMM_WORLD.rank
    size = MPI.COMM_WORLD.size
except ImportError:
    rank = 0
    size = 1
    
default_mp_kwargs = \
{
 'diagonal': 'lower', 
 'keep_diagonal': True, 
 'panel_size': (0.5,0.5), 
 'padding': (0,0)
}    

numerical_types = [float, np.float64, np.float32, int, np.int32, np.int64]

# Machine precision
MP = np.finfo(float).eps

def patch_pinfo(pars):
    # This should be deprecated in future versions
    new_pars = []
    for par in pars:

        if par in tanh_gjah_to_ares:
            new_pars.append(tanh_gjah_to_ares[par])
        elif par in fcoll_gjah_to_ares:
            new_pars.append(fcoll_gjah_to_ares[par])
        else:
            new_pars.append(par)
    
    return new_pars

def err_str(label, mu, err, log, labels=None):
    s = undo_mathify(make_label(label, log, labels))

    s += '={0:.3g}^{{+{1:.2g}}}_{{-{2:.2g}}}'.format(mu, err[1], err[0])
    
    return r'${!s}$'.format(s)

class ModelSubSet(object):
    def __init__(self):
        pass

class ModelSet(BlobFactory):
    def __init__(self, data, subset=None, verbose=True):
        """
        Parameters
        ----------
        data : instance, str
            prefix for a bunch of files ending in .chain.pkl, .pinfo.pkl, etc.,
            or a ModelSubSet instance.

        subset : list, str
            List of parameters / blobs to recover from individual files. Can
            also set subset='all', and we'll try to automatically track down
            all that are available.

        """
        
        self.subset = subset
                
        # Read in data from file (assumed to be pickled)
        if isinstance(data, basestring):
            
            # Check to see if perhaps this is just the chain
            if re.search('pkl', data):
                self._prefix_is_chain = True
                pre_pkl = data[0:data.rfind('.pkl')]
                self.prefix = prefix = pre_pkl
            else:
                self._prefix_is_chain = False
                self.prefix = prefix = data

            i = prefix.rfind('/') # forward slash index

            # This means we're sitting in the right directory already
            if i == - 1:
                self.path = './'
                self.fn = prefix
            else:
                self.path = prefix[0:i+1]
                self.fn = prefix[i+1:]

            if verbose:
                try:
                    print_model_set(self)
                except:
                    pass
                    
        elif isinstance(data, ModelSet):
            self.prefix = data.prefix
            self._chain = data.chain
            self._is_log = data.is_log
            self._base_kwargs = data.base_kwargs

        else:
            raise TypeError('Argument must be ModelSubSet instance or filename prefix')              

    #@property
    #def derived_blobs(self):
    #    if not hasattr(self, '_derived_blobs'):
    #        self._derived_blobs = DQ(self)
    #    return self._derived_blobs
            
        #try:
        #    self._fix_up()
        #except AttributeError:
        #    pass

    @property
    def dust(self):
        if not hasattr(self, '_dust'):
            self._dust = DustCorrection(**self.pf)
        return self._dust

    @property
    def mask(self):
        if not hasattr(self, '_mask'):
            self._mask = np.zeros_like(self.chain) # chain.shape[0]?
        return self._mask
    
    @mask.setter
    def mask(self, value):
        if self.is_mcmc:
            assert len(value) == len(self.logL)
            
            # Must be re-initialized to reflect new mask
            del self._chain, self._logL
        
        self._mask = value

    @property
    def skip(self):
        if not hasattr(self._skip):
            self._skip = 0
        return self._skip
        
    @skip.setter
    def skip(self, value):
        
        if hasattr(self, '_skip'):
            pass
            #print("WARNING: Running `skip` for (at least) the second time!")
        else:
            # On first time, stash away a copy of the original mask
            if not hasattr(self, '_original_mask'):
                self._original_mask = self.mask.copy()
                
            if hasattr(self, '_stop'):
                mask = self.mask.copy()
                assert value < self._stop
            else:    
                mask = self._original_mask.copy()    
        
        self._skip = int(value)
        
        x = np.arange(0, self.logL.size)
        
        mask[x < self._skip] = True
        print("Masked out {} elements using `skip`.".format(self._skip))
        self.mask = mask
        
    @property
    def stop(self):
        if not hasattr(self._stop):
            self._stop = 0
        return self._stop
    
    @stop.setter
    def stop(self, value):
        
        if hasattr(self, '_stop'):
            pass
            #print("WARNING: Running `stop` for (at least) the second time!")
        else:
            # On first time, stash away a copy of the original mask
            if not hasattr(self, '_original_mask'):
                self._original_mask = self.mask.copy()
            
            # If skip has already been called, operate on pre-existing mask.
            # Otherwise, start from scratch
            if hasattr(self, '_skip'):
                mask = self.mask.copy()
                assert value > self._skip
            else:    
                mask = self._original_mask.copy()    
                                
        self._stop = int(value)
    
        x = np.arange(0, self.logL.size)
            
        print("Masked out {} elements using `stop`.".format(max(x) - self._stop))    
        self.mask = mask
        
    def reset_mask(self):
        if hasattr(self, '_skip'):
            del self._skip
        
        if hasattr(self, '_stop'):
            del self._stop
        
    @property
    def load(self):
        if not hasattr(self, '_load'):
            print("WARNING: if this run was restarted, the `load` values " +\
                "are probably wrong.")
            if os.path.exists('{!s}.load.pkl'.format(self.prefix)):
                self._load = concatenate(read_pickle_file(\
                    '{!s}.load.pkl'.format(self.prefix), nloads=None,\
                    verbose=False))
            else:
                self._load = None

        return self._load

    @property
    def pf(self):
        return self.base_kwargs

    @property
    def base_kwargs(self):
        if not hasattr(self, '_base_kwargs'):  
            
            burn = self.prefix.endswith('.burn')
            if burn:
                pre = self.prefix.replace('.burn', '')
            else:
                pre = self.prefix
                      
            if os.path.exists('{!s}.binfo.pkl'.format(pre)):
                fn = '{!s}.binfo.pkl'.format(pre)
            elif os.path.exists('{!s}.setup.pkl'.format(pre)):
                fn = '{!s}.setup.pkl'.format(pre)
            else:    
                self._base_kwargs = None
                return self._base_kwargs
            
            try:
                self._base_kwargs =\
                    read_pickle_file(fn, nloads=1, verbose=False)
            except ImportError as err:
                raise err
            except:
                self._base_kwargs = {}
            
        return self._base_kwargs    

    @property
    def parameters(self):
        # Read parameter names and info
        if not hasattr(self, '_parameters'):
            
            burn = self.prefix.endswith('.burn')
            if burn:
                pre = self.prefix.replace('.burn', '')
            else:
                pre = self.prefix
            
            if os.path.exists('{!s}.pinfo.pkl'.format(pre)):
                (self._parameters, self._is_log) =\
                    read_pickle_file('{!s}.pinfo.pkl'.format(pre), nloads=1,\
                    verbose=False)
                self._parameters = patch_pinfo(self._parameters)
            elif os.path.exists('{!s}.hdf5'.format(self.prefix)):
                f = h5py.File('{!s}.hdf5'.format(self.prefix))
                self._parameters = list(f['chain'].attrs.get('names'))
                #self._is_log = list(f['chain'].attrs.get('is_log'))
                self._is_log = [False] * len(self._parameters)
                f.close()
            else:
                self._is_log = [False] * self.chain.shape[-1]
                self._parameters = ['p{}'.format(i) \
                    for i in range(self.chain.shape[-1])]
        
            self._is_log = tuple(self._is_log)
            self._parameters = tuple(self._parameters)
        
        return self._parameters
        
    @property
    def nwalkers(self):
        # Read parameter names and info
        if not hasattr(self, '_nwalkers'):
            burn = self.prefix.endswith('.burn')
            if burn:
                pre = self.prefix.replace('.burn', '')
            else:
                pre = self.prefix
                
            if os.path.exists('{!s}.rinfo.pkl'.format(pre)):
                loaded =\
                    read_pickle_file('{!s}.rinfo.pkl'.format(pre),\
                    nloads=1, verbose=False)
                self._nwalkers, self._save_freq, self._steps = \
                    list(map(int, loaded))
            else:
                self._nwalkers = self._save_freq = self._steps = None
    
        return self._nwalkers
    
    @property
    def save_freq(self):
        if not hasattr(self, '_save_freq'):
            nwalkers = self.nwalkers
        return self._save_freq
    
    @property
    def steps(self):
        if not hasattr(self, '_steps'):
            nwalkers = self.nwalkers
        return self._steps
    
    @property
    def priors(self):
        if not hasattr(self, '_priors'):   
            if os.path.exists('{!s}.priors.pkl'.format(self.prefix)):
                self._priors =\
                    read_pickle_file('{!s}.priors.pkl'.format(self.prefix),\
                    nloads=1, verbose=False)
            else:
                self._priors = {}
                
        return self._priors    
        
    @property
    def is_log(self):
        if not hasattr(self, '_is_log'):
            pars = self.parameters
        
        return self._is_log
        
    @property
    def polygon(self):
        if not hasattr(self, '_polygon'):
            return None
        return self._polygon
    
    @polygon.setter
    def polygon(self, value):
        self._polygon = value
    
    @property
    def is_mcmc(self):
        if not hasattr(self, '_is_mcmc'):
            if os.path.exists('{!s}.logL.pkl'.format(self.prefix)):
                self._is_mcmc = True
            elif glob.glob('{!s}.dd*.logL.pkl'.format(self.prefix)):
                self._is_mcmc = True    
            else:
                self._is_mcmc = False

        return self._is_mcmc

    @property
    def facc(self):
        if not hasattr(self, '_facc'):
            if os.path.exists('{!s}.facc.pkl'.format(self.prefix)):
                self._facc =\
                    read_pickle_file('{!s}.facc.pkl'.format(self.prefix),\
                    nloads=None, verbose=False)
                self._facc = np.array(self._facc)
            else:
                self._facc = None
        
        return self._facc
                        
    def get_ax(self, ax=None, fig=1):
        if ax is None:
            gotax = False
            fig = pl.figure(fig)
            ax = fig.add_subplot(111)
        else:
            gotax = True
        
        return ax, gotax
            
    @property
    def timing(self):
        if not hasattr(self, '_timing'):
            self._timing = []
            
            i = 1
            fn = '{0!s}.timing_{1!s}.pkl'.format(self.prefix, str(i).zfill(4))
            while os.path.exists(fn):
                self._timing.extend(\
                    read_pickle_file(fn, nloads=None, verbose=False))
                i += 1
                fn = '{0!s}.timing_{1!s}.pkl'.format(self.prefix,\
                    str(i).zfill(4))  
                
                
        return self._timing
            
    @property
    def Nd(self):
        if not hasattr(self, '_Nd'):
            try:
                self._Nd = int(self.chain.shape[-1])       
            except TypeError:
                self._Nd = None
        
        return self._Nd
    
    def last_n_checkpoints(self, num):
        return self.saved_checkpoints[-num:]

    def last_checkpoint(self):
        return self.saved_checkpoints[-1]
    
    @property
    def unique_samples(self):
        if not hasattr(self, '_unique_samples'):
            self._unique_samples = \
                [np.unique(self.chain[:,i].data) for i in range(self.Nd)]
        return self._unique_samples
    
    @property
    def include_checkpoints(self):
        if not hasattr(self, '_include_checkpoints'):
            self._include_checkpoints = None
        return self._include_checkpoints
        
    @include_checkpoints.setter
    def include_checkpoints(self, value):
        assert type(value) in [int, list, tuple, np.ndarray], \
            "Supplied checkpoint(s) must be integer or iterable of integers!"
            
        if type(value) is int:
            self._include_checkpoints = [value]
        else:        
            self._include_checkpoints = value
            
        if hasattr(self, '_chain'):
            print("WARNING: the chain has already been read. Be sure to " +\
                "delete `_chain` attribute before continuing.")

    @property
    def largest_checkpoint(self):
        if not hasattr(self, '_largest_checkpoint'):
            self._largest_checkpoint = max(self.saved_checkpoints)
        return self._largest_checkpoint
    
    @property
    def saved_checkpoints(self):
        """
        The sorted checkpoint numbers of data files saved in the prefix
        associated with this ModelSet. This property uses the counting
        convention which starts with zero.
        """
        if not hasattr(self, '_saved_checkpoints'):
            fns = glob.glob(self.prefix + '.dd*.chain.pkl')
            self._saved_checkpoints = [(int(fn[-14:-10])) for fn in fns]
            self._saved_checkpoints = sorted(self._saved_checkpoints)
            self._saved_checkpoints = np.array(self._saved_checkpoints)
        return self._saved_checkpoints

    @property
    def chain(self):
        # Read MCMC chain
        if not hasattr(self, '_chain'):
            have_chain_f = os.path.exists('{!s}.chain.pkl'.format(self.prefix))
            have_f = os.path.exists('{!s}.pkl'.format(self.prefix))

            if have_chain_f or have_f:
                if have_chain_f:
                    fn = '{!s}.chain.pkl'.format(self.prefix)
                else:
                    fn = '{!s}.pkl'.format(self.prefix)
                
                if rank == 0:
                    print("Loading {!s}...".format(fn))

                t1 = time.time()
                self._chain = read_pickled_chain(fn)
                t2 = time.time()

                if rank == 0:
                    print("Loaded {0!s} in {1:.2g} seconds.\n".format(fn,\
                        t2-t1))
                        
                if hasattr(self, '_mask'):
                    if self.mask.ndim == 1:
                        mask2d = np.array([self.mask] * self._chain.shape[1]).T
                    elif self.mask.ndim == 2:
                        mask2d = self.mask
                        #mask2d = np.zeros_like(self._chain)
                else:
                    mask2d = 0        
                    
                self._chain = np.ma.array(self._chain, mask=mask2d)
            
            # We might have data stored by processor
            elif os.path.exists('{!s}.000.chain.pkl'.format(self.prefix)):
                i = 0
                full_chain = []
                full_mask = []
                fn = '{!s}.000.chain.pkl'.format(self.prefix)
                while True:
                                        
                    if not os.path.exists(fn):
                        break
                    
                    try:
                        this_chain = read_pickled_chain(fn)
                        full_chain.extend(this_chain.copy())
                    except ValueError:
                        #import pickle
                        #f = open(fn, 'rb')
                        #data = pickle.load(f)
                        #f.close()
                        #print data
                        print("Error loading {!s}.".format(fn))
                    
                    i += 1
                    fn = '{0!s}.{1!s}.chain.pkl'.format(self.prefix,\
                        str(i).zfill(3))  
                    
                self._chain = np.ma.array(full_chain, 
                    mask=np.zeros_like(full_chain))

                # So we don't have to stitch them together again.
                # THIS CAN BE REALLY CONFUSING IF YOU, E.G., RUN A NEW
                # CALCULATION AND FORGET TO CLEAR OUT OLD FILES.
                # Hence, it is commented out (for now).
                #if rank == 0:
                #    write_pickle_file(self._chain,\
                #        '{!s}.chain.pkl'.format(self.prefix), ndumps=1,\
                #        open_mode='w', safe_mode=False, verbose=False)

            elif os.path.exists('{!s}.hdf5'.format(self.prefix)):
                f = h5py.File('{!s}.hdf5'.format(self.prefix))
                chain = f['chain'].value
                    
                if hasattr(self, '_mask'):
                    if self.mask.ndim == 1:
                        mask2d = np.array([self.mask] * chain.shape[1]).T
                    else:
                        mask2d = self.mask#np.zeros_like(self._chain)
                else:
                    mask2d = np.zeros(chain.shape)    
                    self.mask = mask2d
                                                                    
                self._chain = np.ma.array(chain, mask=mask2d)
                f.close()

            # If each "chunk" gets its own file.
            elif glob.glob('{!s}.dd*.chain.pkl'.format(self.prefix)):
                
                if self.include_checkpoints is not None:
                    outputs_to_read = []
                    for output_num in self.include_checkpoints:
                        dd = str(output_num).zfill(4)
                        fn = '{0!s}.dd{1!s}.chain.pkl'.format(self.prefix, dd)
                        outputs_to_read.append(fn)
                else:
                    # Only need to use "sorted" on the second time around
                    outputs_to_read = sorted(glob.glob(\
                        '{!s}.dd*.chain.pkl'.format(self.prefix)))
                                
                full_chain = []
                if rank == 0:
                    print("Loading {!s}.dd*.chain.pkl...".format(self.prefix))
                    t1 = time.time()
                for fn in outputs_to_read:
                    if not os.path.exists(fn):
                        print("Found no output: {!s}".format(fn))
                        continue
                    this_chain = read_pickled_chain(fn)
                    full_chain.extend(this_chain)
                    
                self._chain = np.ma.array(full_chain, mask=0)
                
                if rank == 0:
                    t2 = time.time()
                    print("Loaded {0!s}.dd*.chain.pkl in {1:.2g} s.".format(\
                        self.prefix, t2 - t1))
            else:
                self._chain = None            

        return self._chain        
        
    def identify_bad_walkers(self, tol=1e-2, axis=0):
        """
        Find trajectories that are flat. They are probably walkers stuck
        in some "no man's land" region of parameter space. Poor guys.
        
        Returns
        -------
        Lists of walker ID numbers. First, the good walkers, then the bad.
        """
        
        bad_walkers = []
        good_walkers = []
        mask = np.zeros_like(self.chain, dtype=int)
        for i in range(self.nwalkers):
            chain, elements = self.get_walker(i)
            if np.allclose(np.diff(chain[:,axis]), 0.0, atol=tol, rtol=0):
                bad_walkers.append(i)
                mask += elements
            else:
                good_walkers.append(i)
                        
        return good_walkers, bad_walkers, np.minimum(mask, 1)
        
    @property
    def checkpoints(self):
        # Read MCMC chain
        if not hasattr(self, '_checkpoints'):
            i = 0
            fail = 0
            self._checkpoints = {}
            fn = '{!s}.000.checkpt.pkl'.format(self.prefix)
            while True:
            
                if not os.path.exists(fn):
                    fail += 1
                    
                    if fail > 10:
                        break
                else:
                    self._checkpoints[i] =\
                        read_pickle_file(fn, nloads=1, verbose=False)
            
                i += 1
                fn = '{0!s}.{1!s}.checkpt.pkl'.format(self.prefix,\
                    str(i).zfill(3))
                
        return self._checkpoints  
    
    @property
    def logL(self):
        if not hasattr(self, '_logL'):            
            if os.path.exists('{!s}.logL.pkl'.format(self.prefix)):
                self._logL = \
                    read_pickled_logL('{!s}.logL.pkl'.format(self.prefix))
                
                if self.mask.ndim == 2:
                    N = self.chain.shape[0]
                    mask1d = np.array([np.max(self.mask[i,:]) for i in range(N)])
                else:
                    mask1d = self.mask
                self._logL = np.ma.array(self._logL, mask=mask1d)
                
            elif os.path.exists('{!s}.000.logL.pkl'.format(self.prefix)):
                i = 0
                full_logL = []
                full_mask = []
                fn = '{!s}.000.logL.pkl'.format(self.prefix)
                while True:
            
                    if not os.path.exists(fn):
                        break
            
                    try:
                        this_logL = read_pickled_logL(fn)
                        full_logL.extend(this_logL.copy())
                    except ValueError:
                        print("Error loading {!s}.".format(fn))
            
                    i += 1
                    fn = '{0!s}.{1!s}.logL.pkl'.format(self.prefix,\
                        str(i).zfill(3))  
            
                self._logL = np.ma.array(full_logL, 
                    mask=np.zeros_like(full_logL))    
            
            elif glob.glob('{!s}.dd*.logL.pkl'.format(self.prefix)):
                if self.include_checkpoints is not None:
                    outputs_to_read = []
                    for output_num in self.include_checkpoints:
                        dd = str(output_num).zfill(4)
                        fn = '{0!s}.dd{1!s}.logL.pkl'.format(self.prefix, dd)
                        outputs_to_read.append(fn)
                else:
                    outputs_to_read = sorted(glob.glob(\
                        '{!s}.dd*.logL.pkl'.format(self.prefix)))
                
                full_chain = []
                for fn in outputs_to_read:
                    if not os.path.exists(fn):
                        print("Found no output: {!s}".format(fn))
                        continue
                        
                    full_chain.extend(read_pickled_logL(fn))
                        
                if self.mask.ndim == 2:
                    N = self.chain.shape[0]
                    mask1d = np.array([np.max(self.mask[i,:]) for i in range(N)])
                    self._logL = np.ma.array(full_chain, mask=mask1d)
                else:
                    self._logL = np.ma.array(full_chain, mask=self.mask)
            else:
                self._logL = None
                
        return self._logL
    
    @logL.setter
    def logL(self, value):
        self._logL = value
        
    @property
    def L(self):
        if not hasattr(self, '_L'):
            self._L = np.exp(self.logL)
        
        return self._L    
        
    @property
    def betas(self):
        if not hasattr(self, '_betas'):
            if os.path.exists('{!s}.betas.pkl'.format(self.prefix)):
                self._betas =\
                    read_pickled_logL('{!s}.betas.pkl'.format(self.prefix))
            else:
                self._betas = None
        
        return self._betas
                
    @property
    def fails(self):
        if not hasattr(self, '_fails'):
            if os.path.exists('{!s}.fails.pkl'.format(self.prefix)):
                self._fails =\
                    read_pickle_file('{!s}.fails.pkl'.format(self.prefix),\
                    nloads=1, verbose=False)
            elif os.path.exists('{!s}.000.fail.pkl'.format(self.prefix)):
                i = 0
                fails = []
                fn =\
                    '{0!s}.{1!s}.fail.pkl'.format(self.prefix, str(i).zfill(3))
                while True:
                        
                    if not os.path.exists(fn):
                        break
            
                    data = read_pickle_file(fn, nloads=None, verbose=False)
                    
                    fails.extend(data)                 
            
                    i += 1
                    fn = '{0!s}.{1!s}.fail.pkl'.format(self.prefix,\
                        str(i).zfill(3))
                        
                # So we don't have to stitch them together again.
                # AVOIDING CONFUSION
                #if rank == 0:
                #    write_pickle_file(fails,\
                #        '{!s}.fails.pkl'.format(self.prefix), ndumps=1,\
                #        open_mode='w', safe_mode=False, verbose=False)
                    
                self._fails = fails    
                
            else:
                self._fails = None
            
        return self._fails
    
    @property
    def timeouts(self):
        if not hasattr(self, '_timeouts'):
            if os.path.exists('{!s}.timeout.pkl'.format(self.prefix)):
                self._fails =\
                    read_pickle_file('{!s}.timeout.pkl'.format(self.prefix),\
                    nloads=1, verbose=False)
            elif os.path.exists('{!s}.000.timeout.pkl'.format(self.prefix)):
                i = 0
                timeout = []
                fn = '{0!s}.{1!s}.timeout.pkl'.format(self.prefix,\
                    str(i).zfill(3))
                while True:
    
                    if not os.path.exists(fn):
                        break
    
                    data = read_pickle_file(fn, nloads=None, verbose=False)
                    timeout.extend(data)                 
    
                    i += 1
                    fn = '{0!s}.{1!s}.timeout.pkl'.format(self.prefix,\
                        str(i).zfill(3))
    
                self._timeout = timeout    
    
            else:
                self._timeout = None
    
        return self._timeout    
    
    def get_walker(self, num):
        """
        Return chain elements corresponding to specific walker.
        
        Parameters
        ----------
        num : int
            ID # for walker of interest.
            
        Returns
        -------
        1. 2-D array with shape (nsteps, nparameters).
        2. A mask, with the same shape as the chain, with elements == 1 
           corresponding to those specific to the given walker.
        
        """
        
        sf = self.save_freq
        nw = self.nwalkers
        
        assert num < nw, "Only {} walkers were used!".format(nw)
        
        steps_per_walker = self.chain.shape[0] // nw
        nchunks = steps_per_walker // sf
        
        # "size" of each chunk in # of MCMC steps
        schunk = nw * sf 
        
        data = []
        elements = np.zeros_like(self.chain, dtype=int).data
        for i in range(nchunks):   
            chunk = self.chain[i*schunk + sf*num:i*schunk + sf*(num+1)]
            elements[i*schunk + sf*num:i*schunk + sf*(num+1)] = 1
            data.extend(chunk)
            
        return np.array(data), elements
                
    @property
    def Npops(self):
        if not hasattr(self, '_Npops') and self.base_kwargs is not None:
            self._Npops = count_populations(**self.base_kwargs)
        elif self.base_kwargs is None:
            self._Npops = 1
    
        return self._Npops
    
    def _fix_up(self):
        
        if not hasattr(self, 'blobs'):
            return
        
        if not hasattr(self, 'chain'):
            return
        
        if self.blobs.shape[0] == self.chain.shape[0]:
            return
            
        # Force them to be the same shape. The shapes might mismatch if
        # one processor fails to write to disk (or just hasn't quite yet),
        # or for more pathological reasons I haven't thought of yet.
                        
        if self.blobs.shape[0] > self.chain.shape[0]:
            tmp = self.blobs[0:self.chain.shape[0]]
            self.blobs = tmp
        else:
            tmp = self.chain[0:self.blobs.shape[0]]
            self.chain = tmp
    
    #def _load(self, fn):
    #    if os.path.exists(fn):
    #        return read_pickle_file(fn, nloads=1, verbose=False)
    
    @property    
    def blob_redshifts_float(self):
        if not hasattr(self, '_blob_redshifts_float'):
            self._blob_redshifts_float = []
            for i, redshift in enumerate(self.blob_redshifts):
                if isinstance(redshift, basestring):
                    self._blob_redshifts_float.append(None)
                else:
                    self._blob_redshifts_float.append(round(redshift, 3))
            
        return self._blob_redshifts_float
    
    @property    
    def blob_redshifts_float(self):
        if not hasattr(self, '_blob_redshifts_float'):
            self._blob_redshifts_float = []
            for i, redshift in enumerate(self.blob_redshifts):
                if isinstance(redshift, basestring):
                    z = None
                else:
                    z = redshift
                    
                self._blob_redshifts_float.append(z)
            
        return self._blob_redshifts_float
    
    def SelectModels(self):
        """
        Draw a rectangle on supplied matplotlib.axes.Axes instance, return
        information about those models.
        """
                
        if not hasattr(self, '_ax'):
            raise AttributeError('No axis found.')
                
        self._op = self._ax.figure.canvas.mpl_connect('button_press_event', 
            self._on_press)
        self._or = self._ax.figure.canvas.mpl_connect('button_release_event', 
            self._on_release)
                            
    def _on_press(self, event):
        self.x0 = event.xdata
        self.y0 = event.ydata
        
    def _on_release(self, event):
        self.x1 = event.xdata
        self.y1 = event.ydata
        
        self._ax.figure.canvas.mpl_disconnect(self._op)
        self._ax.figure.canvas.mpl_disconnect(self._or)
        
        # Width and height of rectangle
        dx = abs(self.x1 - self.x0)
        dy = abs(self.y1 - self.y0)
        
        # Find lower left corner of rectangle
        lx = self.x0 if self.x0 < self.x1 else self.x1
        ly = self.y0 if self.y0 < self.y1 else self.y1
        
        # Lower-left
        ll = (lx, ly)
        
        # Upper right
        ur = (lx + dx, ly + dy)
    
        origin = (self.x0, self.y0)
        rect = Rectangle(ll, dx, dy, fc='none', ec='k')
        
        self._ax.add_patch(rect)
        self._ax.figure.canvas.draw()
        
        print('{0:f} {1:f} {2:f} {3:f}'.format(lx, lx+dx, ly, ly+dy))
        
        self.Slice((lx, lx+dx, ly, ly+dy), **self.plot_info)
    
    def SliceIteratively(self, pars):
        #assert self.Nd == 3 # for now
        
        if type(pars) != list:
                        
            par = pars
            k = list(self.parameters).index(par)
            vals = self.unique_samples[k]
            
            slices = []
            for i, val in enumerate(vals):
                            
                if i == 0:
                    lo = 0
                    hi = np.mean([val, vals[i+1]])
                elif i == len(vals) - 1:
                    lo = np.mean([val, vals[i-1]])
                    hi = max(vals) * 1.1
                else:
                    lo = np.mean([vals[i-1], val])  
                    hi = np.mean([vals[i+1], val])  
                    
                slices.append(self.Slice([lo, hi], [par]))
            
            return vals, slices
        else:
            vals
            for par in pars:
                k = list(self.parameters).index(par)
                vals.append(np.sort(np.unique(self.chain[:,k])))    
                
    def Slice(self, constraints, pars, ivar=None, take_log=False, 
        un_log=False, multiplier=1.):
        """
        Return revised ("sliced") dataset given set of criteria.
    
        Parameters
        ----------
        constraints : list, tuple
            A rectangle (or line segment) bounding the region of interest. 
            For 2-D plane, supply (left, right, bottom, top), and then to
            `pars` supply list of datasets defining the plane. For 1-D, just
            supply (min, max).
        pars:
            Dictionary of constraints to use to calculate likelihood.
            Each entry should be a two-element list, with the first
            element being the redshift at which to apply the constraint,
            and second, a function for the posterior PDF for that quantity.s
    
        Examples
        --------
    
        Returns
        -------
        Object to be used to initialize a new ModelSet instance.
    
        """
        
        if len(constraints) == 4:
            Nd = 2
            x1, x2, y1, y2 = constraints
        else:
            Nd = 1
            x1, x2 = constraints
    
        # Figure out what these values translate to.
        data = self.ExtractData(pars, ivar, take_log, un_log, 
            multiplier)

        # Figure out elements we want
        xok_ = np.logical_and(data[pars[0]] >= x1, data[pars[0]] <= x2)
        xok_MP = np.logical_or(np.abs(data[pars[0]] - x1) <= MP, 
            np.abs(data[pars[0]].data - x2) <= MP)
        xok_pre = np.logical_or(xok_, xok_MP)
        
        unmasked = np.logical_not(data[pars[0]].mask == 1)
        xok = np.logical_and(xok_pre, unmasked)

        if Nd == 2:
            yok_ = np.logical_and(data[pars[1]] >= y1, data[pars[1]] <= y2)
            yok_MP = np.logical_or(np.abs(data[pars[1]] - y1) <= MP,
                np.abs(data[pars[1]] - y2) <= MP)
            yok = np.logical_or(yok_, yok_MP)
            to_keep = np.logical_and(xok, yok)
        else:
            to_keep = np.array(xok)

        mask = np.logical_not(to_keep)
        
        ##
        # CREATE NEW MODELSET INSTANCE
        ##
        model_set = ModelSet(self.prefix)
        
        # Set the mask.
        # Must this be 2-D?
        mask2d = np.array([mask] * self.chain.shape[1]).T
        model_set.mask = np.logical_or(mask2d, self.mask)
                
        i = 0
        while hasattr(self, 'slice_{}'.format(i)):
            i += 1

        setattr(self, 'slice_{}'.format(i), model_set)

        print("Saved result to slice_{} attribute.".format(i))

        return model_set
        
    def SliceByElement(self, to_keep):
        
        ##
        # CREATE NEW MODELSET INSTANCE
        ##
        model_set = ModelSet(self.prefix)
        
        # Set the mask! 
        keep = np.zeros(self.chain.shape[0])
        for i in to_keep:
            keep[i] = 1
                                
        old_keep = np.logical_not(self.mask)[:,0]    
                
        model_set.mask = np.logical_not(np.logical_and(keep, old_keep))
                
        return model_set                                       
        
    def SliceByParameters(self, to_keep):
        
        elements = []
        for kw in to_keep:
            tmp = []
            for i, par in enumerate(self.parameters):
                if self.is_log[i]:
                    tmp.append(np.log10(kw[par]))
                else:
                    tmp.append(kw[par])
                    
            tmp = np.array(tmp)
            loc = np.argwhere(self.chain == tmp)[:,0]
            
            if not loc:
                continue
            
            assert np.all(np.diff(loc) == 0)
            elements.append(loc[0])
        
        return self.SliceByElement(elements)        
        
    def difference(self, set2):
        """
        Create a new ModelSet out of the elements unique to current ModelSet.
        """
        
        assert self.chain.shape == set2.chain.shape
        assert self.parameters == set2.parameters
        
        mask = np.ones(self.chain.shape[0])
        for i, element in enumerate(self.chain):
            if self.mask[i] == 0 and (set2.mask[i] == 1):
                mask[i] = 0

        model_set = ModelSet(self.prefix)
        
        # Set the mask! 
        model_set.mask = mask
                        
        return model_set        
    
    def union(self, set2):
        """
        Create a new ModelSet out of the elements unique to input sets.
        """
    
        assert self.chain.shape == set2.chain.shape
        assert self.parameters == set2.parameters
    
        mask = self.mask * set2.mask
        model_set = ModelSet(self.prefix)
    
        # Set the mask! 
        model_set.mask = mask
    
        return model_set    
        
    def SliceByPolygon(self, parameters, polygon):
        """
        Convert a bounding polygon to a new ModelSet instance.
        
        Parameters
        ----------
        parameters : list
            List of parameters names / blob names defining the (x, y) plane
            of the input polygon.
        polygon : shapely.geometry.Polygon instance
            Yep.
            
        Returns
        -------
        New instance of THIS VERY CLASS.
        
        """
        
        data = self.ExtractData(parameters)
        
        xdata = data[parameters[0]]
        ydata = data[parameters[1]]
        
        assert len(xdata) == len(ydata)
        assert len(xdata) == self.chain.shape[0]
        
        mask = np.zeros(self.chain.shape[0])
        for i in range(len(xdata)):
            pt = geometry.Point(xdata[i], ydata[i])
            
            pt_in_poly = polygon.contains(pt) or polygon.touches(pt) \
                or polygon.intersects(pt)
            
            if not pt_in_poly:
                mask[i] = 1                

        
        ##
        # CREATE NEW MODELSET INSTANCE
        ##
        model_set = ModelSet(self.prefix)
        
        # Set the mask! 
        model_set.mask = np.logical_or(mask, self.mask)
        
        # Save the polygon we used
        model_set.polygon = polygon        
                
        return model_set        
        
    def Vennify(self, polygon1, polygon2):
        """
        Return a new ModelSet instance containing only models that lie 
        within (or outside, if union==False) intersection of two polygons.
        """
        
        overlap = polygon1.intersection(polygon2)
        
        p1_w_overlap = polygon1.union(overlap)
        p2_w_overlap = polygon2.union(overlap)
        
        p1_unique = polygon1.difference(p2_w_overlap)
        p2_unique = polygon2.difference(p1_w_overlap)
        
        return p1_unique, overlap, p2_unique
        
    @property
    def plot_info(self):
        if not hasattr(self, '_plot_info'):
            self._plot_info = None
    
        return self._plot_info
    
    @plot_info.setter
    def plot_info(self, value):
        self._plot_info = value
        
    def WalkerTrajectoriesMultiPlot(self, pars=None, N='all', walkers='first', 
        ax=None, fig=1, mp_kwargs={}, best_fit='mode', ncols=1, **kwargs):
        """
        Plot trajectories of `N` walkers for multiple parameters at once.
        """

        if pars is None:
            pars = self.parameters

        if N == 'all':
            N = self.nwalkers

        Npars = len(pars)
        while (Npars / float(ncols)) % 1 != 0:
            Npars += 1
            
        mp = MultiPanel(dims=(Npars//ncols, ncols), fig=fig, **mp_kwargs)

        w = self._get_walker_subset(N, walkers)

        if not best_fit:
            loc = None
        elif best_fit == 'median':
            N = len(self.logL)
            loc = np.sort(self.logL)[N // 2]
        elif best_fit == 'mode':
            loc = np.argmax(self.logL)

        for i, par in enumerate(pars):
            self.WalkerTrajectories(par, walkers=w, ax=mp.grid[i], **kwargs)

            if loc is None:
                continue

            # Plot current maximum likelihood value
            if par in self.parameters:
                k = self.parameters.index(par)
                mp.grid[i].plot([0, self.chain[:,k].size / float(self.nwalkers)], 
                    [self.chain[loc,k]]*2, color='k', ls='--', lw=5)
            else:
                pass
                
        return mp           
                
    def WalkerTrajectories(self, par, N=50, walkers='first', ax=None, fig=1,
        **kwargs):
        """
        Plot 1-D trajectories of N walkers (i.e., vs. step number).
        
        Parameters
        ----------
        parameter : str
            Name of parameter to show results for.
        walkers : str
            Which walkers to grab? By default, select `N` random walkers,
            but can also grab `N` first or `N` last walkers.
            
        """
        
        if ax is None:
            gotax = False
            fig = pl.figure(fig)
            ax = fig.add_subplot(111)
        else:
            gotax = True
        
        if isinstance(walkers, basestring):
            assert N < self.nwalkers, \
                "Only {} walkers available!".format(self.nwalkers)

            to_plot = self._get_walker_subset(N, walkers)
        else:
            to_plot = walkers
        
        for i in to_plot:
            data, elements = self.get_walker(i)
            if par in self.parameters:
                y = data[:,self.parameters.index(par)]        
            else:
                keep = elements[:,0]
                tmp = self.ExtractData(par)[par]
                y = tmp[keep == 1]
                                                    
            x = np.arange(1, len(y)+1)
            ax.plot(x, y, **kwargs)

        self.set_axis_labels(ax, ['step', par], take_log=False, un_log=False,
            labels={})
            
        return ax
        
    def WalkerTrajectory2D(self, pars, N=50, walkers='first', ax=None, fig=1,
        scale_by_step=True, scatter=False, **kwargs):
        
        if ax is None:
            gotax = False
            fig = pl.figure(fig)
            ax = fig.add_subplot(111)
        else:
            gotax = True
            
        assert type(pars) in [list, tuple]
        par1, par2 = pars
        
        if isinstance(walkers, basestring):
            assert N < self.nwalkers, \
                "Only {} walkers available!".format(self.nwalkers)

            to_plot = self._get_walker_subset(N, walkers)
        else:
            to_plot = walkers
        
        for i in to_plot:
            data, mask = self.get_walker(i)
            
            if scale_by_step:
                if scatter:
                    c = np.arange(0, data[:,0].size, 1)
                else:
                    raise NotImplementedError('dunno how to do this correctly')
                    carr = np.arange(data[:,0].size)
                    c = pl.cm.jet(carr)
                    #cmap = colormap(Normalize(carr.min(), carr.max()))
            else:
                c = None
                
            if scatter:
                ax.scatter(data[:,self.parameters.index(par1)],
                    data[:,self.parameters.index(par2)], c=c, **kwargs)
            else:
                ax.plot(data[:,self.parameters.index(par1)],
                    data[:,self.parameters.index(par2)], color=c, **kwargs)
                
            
        self.set_axis_labels(ax, [par1, par2], take_log=False, un_log=False,
            labels={})
        
        return ax
        
    def _get_walker_subset(self, N=50, walkers='random'):

        to_plot = np.arange(self.nwalkers)
        
        if walkers == 'random':
            np.random.shuffle(to_plot)
            slc = slice(0, N)
        elif walkers == 'first':
            slc = slice(0, N)
        elif walkers == 'last':
            slc = slice(-N, None)
        else:
            raise NotImplementedError('help!')
            
        return to_plot[slc]

    def sort_by_Tmin(self):
        """
        If doing a multi-pop fit, re-assign population ID numbers in 
        order of increasing Tmin.
        
        Doesn't return anything. Replaces attribute 'chain' with new array.
        """

        # Determine number of populations
        tmp_pf = {key : None for key in self.parameters}
        Npops = count_populations(**tmp_pf)

        if Npops == 1:
            return
        
        # Check to see if Tmin is common among all populations or not    
    
    
        # Determine which indices correspond to Tmin, and population #
    
        i_Tmin = []
        
        # Determine which indices 
        pops = [[] for i in range(Npops)]
        for i, par in enumerate(self.parameters):

            # which pop?
            m = re.search(r"\{([0-9])\}", par)

            if m is None:
                continue

            num = int(m.group(1))
            prefix = par.split(m.group(0))[0]
            
            if prefix == 'Tmin':
                i_Tmin.append(i)

        self._unsorted_chain = self.chain.copy()

        # Otherwise, proceed to re-sort data
        tmp_chain = np.zeros_like(self.chain)
        for i in range(self.chain.shape[0]):

            # Pull out values of Tmin
            Tmin = [self.chain[i,j] for j in i_Tmin]
            
            # If ordering is OK, move on to next link in the chain
            if np.all(np.diff(Tmin) > 0):
                tmp_chain[i,:] = self.chain[i,:].copy()
                continue

            # Otherwise, we need to fix some stuff

            # Determine proper ordering of Tmin indices
            i_Tasc = np.argsort(Tmin)
            
            # Loop over populations, and correct parameter values
            tmp_pars = np.zeros(len(self.parameters))
            for k, par in enumerate(self.parameters):
                
                # which pop?
                m = re.search(r"\{([0-9])\}", par)

                if m is None:
                    tmp_pars.append()
                    continue

                pop_num = int(m.group(1))
                prefix = par.split(m.group(0))[0]
                
                new_pop_num = i_Tasc[pop_num]
                
                new_loc = self.parameters.index('{0!s}{{{1}}}'.format(prefix,\
                    new_pop_num))
                
                tmp_pars[new_loc] = self.chain[i,k]

            tmp_chain[i,:] = tmp_pars.copy()
                        
        del self.chain
        self.chain = tmp_chain

    @property
    def cosm(self):
        if not hasattr(self, '_cosm'):
            self._cosm = Cosmology(**self.pf)
        
        return self._cosm
        
    @property
    def derived_blob_ivars(self):
        if not hasattr(self, '_derived_blob_ivars'):
            junk = self.derived_blob_names
        return self._derived_blob_ivars

    @property
    def derived_blob_names(self):
        #if not hasattr(self, '_derived_blob_names'):
        self._derived_blob_ivars = {}
        self._derived_blob_names = []
        fn = '{}.dbinfo.pkl'.format(self.prefix)
        if not os.path.exists(fn):
            return self._derived_blob_names
            
        with open(fn, 'rb') as f:
            ivars = pickle.load(f)
            self._derived_blob_ivars.update(ivars)
            for key in ivars:
                self._derived_blob_names.append(key)
                
        return self._derived_blob_names
        
    def set_constraint(self, add_constraint=False, **constraints):
        """
        For ModelGrid calculations, the likelihood must be supplied 
        after the fact.

        Parameters
        ----------
        add_constraint: bool
            If True, operate with logical and when constructing likelihood.
            That is, this constraint will be applied in conjunction with
            previous constraints supplied.
        constraints : dict
            Constraints to use in calculating logL
            
        Example
        -------
        # Assume redshift of turning pt. D is 15 +/- 2 (1-sigma Gaussian)
        data = {'z': ['D', lambda x: np.exp(-(x - 15)**2 / 2. / 2.**2)]}
        self.set_constraint(**data)
            
        Returns
        -------
        Sets "logL" attribute, which is used by several routines.    
            
        """    

        if add_constraint and hasattr(self, 'logL'):
            pass
        else:    
            self.logL = np.zeros(self.chain.shape[0])

        if hasattr(self, '_weights'):
            del self._weights

        for i in range(self.chain.shape[0]):
            logL = 0.0
            
            if i >= self.blobs.shape[0]:
                break
            
            for element in constraints:

                z, func = constraints[element]
                
                try:
                    j = self.blob_redshifts.index(z)
                except ValueError:
                    ztmp = []
                    for redshift in self.blob_redshifts_float:
                        if redshift is None:
                            ztmp.append(None)
                        else:
                            ztmp.append(round(redshift, 1))    

                    j = ztmp.index(round(z, 1))
                
                if element in self.blob_names:
                    k = self.blob_names.index(element)
                    data = self.blobs[i,j,k]
                else:
                    k = self.derived_blob_names.index(element)
                    data = self.derived_blobs[i,j,k]                

                logL -= np.log(func(data))

            self.logL[i] -= logL

        mask = np.isnan(self.logL)

        self.logL[mask] = -np.inf
        
    def LinePlot(self, pars, ivar=None, ax=None, fig=1, c=None,
        take_log=False, un_log=False, multiplier=1., use_colorbar=False, 
        sort_by='z', filter_z=None, **kwargs):
        ax = self.Scatter(pars, ivar=None, ax=ax, fig=fig, c=c,
            take_log=take_log, un_log=un_log, multiplier=multiplier, 
            use_colorbar=use_colorbar, line_plot=True, sort_by=sort_by, 
            **kwargs)

        return ax

    def Scatter(self, pars, ivar=None, ax=None, fig=1, c=None, aux=None,
        take_log=False, un_log=False, multiplier=1., use_colorbar=True, 
        line_plot=False, sort_by='z', filter_z=None, rungs=False, 
        rung_label=None, rung_label_top=True, return_cb=False, cax=None,
        skip=0, skim=1, stop=None,
        cb_kwargs={}, operation=None, **kwargs):
        """
        Plot samples as points in 2-d plane.
    
        Parameters
        ----------
        pars : list
            2-element list of parameter names. 
        ivar : float, list
            Independent variable(s) to be used for non-scalar blobs.
        z : str, float
            Redshift at which to plot x vs. y, if applicable.
        c : str
            Field for (optional) color axis.

        Returns
        -------
        matplotlib.axes._subplots.AxesSubplot instance.

        """

        if ax is None:
            gotax = False
            fig = pl.figure(fig)
            ax = fig.add_subplot(111)
        else:
            gotax = True

        # Make a new variable since pars might be self.parameters
        # (don't want to modify that)
        if c is not None:
            p = list(pars) + [c]
            if ivar is not None:
                if len(ivar) != 3:
                    iv = list(ivar) + [None]
                else:
                    iv = ivar
            else:
                iv = None
        else:
            p = pars
            iv = ivar
        
        data = self.ExtractData(p, iv, take_log, un_log, multiplier)

        xdata = data[p[0]]
        ydata = data[p[1]]
        
        if aux is not None:
            adata = self.ExtractData(aux)[aux]

        if c is not None:
            _cdata = data[p[2]].squeeze()
            
            if operation is None:
                cdata = _cdata
            elif isinstance(operation, basestring):
                assert self.Nd > 2
                
                # There's gotta be a faster way to do this...
                
                xu = np.unique(xdata[np.isfinite(xdata)])
                yu = np.unique(ydata[np.isfinite(ydata)])
                
                ids = []
                for i, val in enumerate(_cdata):
                    x = xdata[i]
                    y = ydata[i]
                    
                    i = np.argmin(np.abs(x - xu))
                    j = np.argmin(np.abs(y - yu))
                    
                    ids.append(i * len(yu) + j)
                                
                ids = np.array(ids)
                cdata = np.zeros_like(_cdata)
                for i, idnum in enumerate(np.unique(ids)):
                                        
                    #if isinstance(operation, basestring):   
                    tmp = _cdata[ids == idnum]
                    if operation == 'mean':
                        cdata[ids == idnum] = np.mean(tmp)
                    elif operation == 'stdev':
                        cdata[ids == idnum] = np.std(tmp)
                    elif operation == 'diff':
                        cdata[ids == idnum] = np.max(tmp) - np.min(tmp)
                    elif operation == 'max':
                        cdata[ids == idnum] = np.max(tmp) 
                    elif operation == 'min':
                        cdata[ids == idnum] = np.min(tmp) 
                        
                    # The next two could be accomplished by slicing
                    # along third dimension    
                    elif operation == 'first':
                        val = min(adata[adata.mask == 0])
                        cond = np.logical_and(ids == idnum, adata == val)
                        cdata[ids == idnum] = _cdata[cond]
                    elif operation == 'last':
                        val = max(adata[adata.mask == 0])
                        cond = np.logical_and(ids == idnum, adata == val)
                        cdata[ids == idnum] = _cdata[cond]
                    else:
                        raise NotImplementedError('help')
                    #else:
                        
                        #cond = np.ma.logical_and(ids == idnum, adata == operation)
                        #print np.any(adata == operation), np.unique(adata), operation, np.ma.sum(cond)
                        #cdata[ids == idnum] = _cdata[cond]
            else:
                cdata = _cdata        
        else:
            cdata = None

        # Seems unecessary...a method inherited from days past?
        func = ax.__getattribute__('scatter')
            
        if filter_z is not None:
            _condition = np.isclose(cdata, filter_z)
            if not np.any(_condition):
                print("No instances of {0!s}={1:.4g}".format(p[2], filter_z))
                return
            
            xd = xdata[_condition]
            yd = ydata[_condition]
            cd = cdata[_condition]
            
        else:
            _condition = None
            xd = xdata.compressed()
            yd = ydata.compressed()
            
            if cdata is not None:
                cd = cdata.compressed()
            else:
                cd = cdata
                        
        if rungs:
            scat = self._add_rungs(xdata, ydata, cdata, ax, _condition, 
                label=rung_label, label_on_top=rung_label_top, **kwargs)
        elif hasattr(self, 'weights') and cdata is None:
            scat = func(xd, yd, c=self.weights, **kwargs)
        elif line_plot:
            scat = func(xd, yd, **kwargs)
        elif (cdata is not None) and (filter_z is None):
            scat = func(xd, yd, c=cd, **kwargs)
        else:
            scat = func(xd, yd, **kwargs)
                           
        if (cdata is not None) and use_colorbar and (not line_plot) and \
           (filter_z is None):
            if 'facecolors' in kwargs:
                if kwargs['facecolors'] in ['none', None]:
                    cb = None
                else:
                    cb = None
            else:
                cb = self._cb = pl.colorbar(scat, cax=cax, **cb_kwargs)
        else:
            cb = None
        
        self._scat = scat

        # Might use this for slicing 
        self.plot_info = {'pars': pars, 'ivar': ivar,
            'take_log': take_log, 'un_log':un_log, 'multiplier':multiplier}

        # Make labels
        self.set_axis_labels(ax, p, take_log, un_log, cb)

        pl.draw()
        
        self._ax = ax
        
        if return_cb:
            return ax, cb
        else:    
            return ax
        
    def _fix_tick_labels(self, ax):
        tx = list(map(int, ax.get_xticks()))
        ax.set_xticklabels(list(map(str, tx)))
        
        ty = list(map(int, ax.get_yticks()))
        ax.set_yticklabels(list(map(str, ty)))
        
        pl.draw()
        
        return ax
    
    def _add_rungs(self, _x, _y, c, ax, cond, tick_size=1, label=None, 
        label_on_top=True, **kwargs):
    
        assert cond.sum() == 1
        
        # Grab rung locations
        _xr = _x[cond][0]
        _yr = _y[cond][0]
        
        # We need to transform into the "axes fraction" coordinate system
        xr, yr = ax.transData.transform((_xr, _yr))
        
        # Just determine a fixed length scale in data coordinates
        _xx1, _yy1 = ax.transData.transform((_xr, _yr))
        _xx2, _yy2 = ax.transData.transform((_xr+1, _yr))        
        one_in_display_units = abs(_xx2 - _xx1)
        
        data = []
        for i in range(len(_x)):
            data.append(ax.transData.transform((_x[i], _y[i])))
        
        x, y = np.array(data).T

        dy = np.roll(y, -1) - y
        dx = np.roll(x, -1) - x
        
        angle = np.arctan2(dy, dx) + np.pi / 2.
        
        # Set to 1 in data units * some amplification factor
        tick_len = one_in_display_units * tick_size
        
        x2 = xr + tick_len * np.cos(angle[cond])[0]
        x1 = xr - tick_len * np.cos(angle[cond])[0]
        y1 = yr - tick_len * np.sin(angle[cond])[0]
        y2 = yr + tick_len * np.sin(angle[cond])[0]
        
        if label_on_top:
            _xl = xr + 2 * tick_len * np.cos(angle[cond])[0]
            _yl = yr + 2 * tick_len * np.sin(angle[cond])[0]
        else:
            _xl = xr - 2 * tick_len * np.cos(angle[cond])[0]
            _yl = yr - 2 * tick_len * np.sin(angle[cond])[0]
        
        # Transform back into data coordinates!        
        inv = ax.transData.inverted()
        
        rungs = []
        for pt in ([x1, y1], [xr, yr], [x2, y2]):
            rungs.append(inv.transform(pt))
                                                              
        tick_lines = LineCollection([rungs], **kwargs)
        ax.add_collection(tick_lines)
        
        if label is not None:
            xl, yl = inv.transform((_xl, _yl))
            
            rot = (angle[cond][0] + np.pi / 2.) * 180 / np.pi
            pl.text(xl, yl, label, va="center", ha="center", rotation=rot,
                fontsize=12)
                
        return ax
    
    def BoundingPolygon(self, pars, ivar=None, ax=None, fig=1,
        take_log=False, un_log=False, multiplier=1., add_patch=True,
        skip=0, skim=1, stop=None,
        boundary_type='convex', alpha=0.3, return_polygon=False, **kwargs):
        """
        Basically a scatterplot but instead of plotting individual points,
        we draw lines bounding the locations of all those points.
        
        Parameters
        ----------
        pars : list, tuple
            List of parameters that defines 2-D plane.
        boundary_type : str
            Options: 'convex' or 'concave' or 'envelope'
        alpha : float
            Only used if boundary_type == 'concave'. Making alpha smaller
            makes the contouring more crude, but also less noisy as a result.
        
            
        """
        
        assert have_shapely, "Need shapely installed for this to work."
        assert have_descartes, "Need descartes installed for this to work."

        if (ax is None) and add_patch:
            gotax = False
            fig = pl.figure(fig)
            ax = fig.add_subplot(111)
        else:
            gotax = True

        data = self.ExtractData(pars, ivar, take_log, un_log, multiplier)

        xdata = self.xdata = data[pars[0]].compressed()
        ydata = self.ydata = data[pars[1]].compressed()

        # Organize into (x, y) pairs
        points = list(zip(xdata, ydata))

        # Create polygon object
        point_collection = geometry.MultiPoint(list(points))

        if boundary_type == 'convex':
            polygon = point_collection.convex_hull
        elif boundary_type == 'concave':
            polygon, edge_points = self._alpha_shape(points, alpha)
        elif boundary_type == 'envelope':
            polygon = point_collection.envelope
        else:
            raise ValueError('Unrecognized boundary_type={!s}!'.format(\
                boundary_type))        

        # Plot a Polygon using descartes
        if add_patch and (polygon is not None):
            # This basically just gets the axis object in order without
            # actually plotting anything
            self.Scatter(pars, ivar=ivar, take_log=take_log, un_log=un_log,
                multiplier=multiplier, ax=ax, edgecolors='none', 
                facecolors='none')
            
            try:        
                patch = PolygonPatch(polygon, **kwargs)
                ax.add_patch(patch)
            except:
                patches = []
                for pgon in polygon:
                    patches.append(PolygonPatch(pgon, **kwargs))
                
                try:
                    ax.add_collection(PatchCollection(patches, match_original=True))
                except TypeError:
                    print('Patches: {!s}'.format(patches))

            pl.draw()

        if return_polygon and add_patch:
            return ax, polygon
        elif return_polygon:
            return polygon
        else:
            return ax

    def get_par_prefix(self, par):
        m = re.search(r"\{([0-9])\}", par)

        if m is None:
            return par

        # Population ID number
        num = int(m.group(1))

        # Pop ID including curly braces
        prefix = par.split(m.group(0))[0]
    
        return prefix
    
    @property
    def weights(self):        
        if (not self.is_mcmc) and hasattr(self, 'logL') \
            and (not hasattr(self, '_weights')):
            self._weights = np.exp(self.logL)

        return self._weights

    def get_levels(self, L, nu=[0.95, 0.68]):
        """
        Return levels corresponding to input nu-values, and assign
        colors to each element of the likelihood.
        """
    
        nu, levels = _error_2D_crude(L, nu=nu)
                                                                      
        return nu, levels
        
    def PruneSet(self, pars, bin_edges, N, ivar=None, take_log=False,
        un_log=False, multiplier=1.):
        """
        Take `N` models from each 2-D bin in space `pars`.
        """
        
        data = self.ExtractData(pars, ivar=ivar, 
            take_log=take_log, un_log=un_log, multiplier=multiplier)
        
        be = bin_edges
        ct = np.zeros([len(be[0]) - 1, len(be[1]) - 1])
        out = np.zeros([len(be[0]) - 1, len(be[1]) - 1, N])
        
        for h in range(self.chain.shape[0]):
            x = data[pars[0]][h]
            y = data[pars[1]][h]
            
            if (x < be[0][0]) or (x > be[0][-1]):
                continue
            if (y < be[1][0]) or (y > be[1][-1]):
                continue    
                
            # Find bin where this model lives.
            i = np.argmin(np.abs(x - be[0]))
            j = np.argmin(np.abs(y - be[1]))
            
            if i == len(be[0]) - 1:
                i -= 1
            if j == len(be[1]) - 1:
                j -= 1
            
            # This bin is already full
            if ct[i,j] == N:
                continue
                
            k = ct[i,j]
            out[i,j,k] = h    
            ct[i,j] += 1
        
        # Create a new object
        to_keep = out.ravel()
        return self.SliceByElement(to_keep)
    
    def get_1d_error(self, par, ivar=None, nu=0.68, take_log=False,
        limit=None, un_log=False, multiplier=1., peak='median', skip=0,
        stop=None):
        """
        Compute 1-D error bar for input parameter.
        
        Parameters
        ----------
        par : str
            Name of parameter. 
        nu : float
            Percent likelihood enclosed by this 1-D error
        peak : str
            Determines whether the 'best' value is the median, mode, or
            maximum likelihood point.
            
        Returns
        -------
        if peak is None:
            Returns x-values corresponding to desired quartile range, i.e.,
            not really an error-bar.
        else:
            tuple: (maximum likelihood value, negative error, positive error).
        """

        to_hist = self.ExtractData(par, ivar=ivar, take_log=take_log, 
            multiplier=multiplier, un_log=un_log)

        # Need to weight results of non-MCMC runs explicitly
        if not hasattr(self, '_weights'):
            weights = None
        else:
            weights = self.weights

        # Apply mask to weights
        if weights is not None and to_hist[par].shape != weights.shape:
            weights = weights[np.logical_not(mask)]

        if stop is not None:
            stop = -int(stop)
                                
        if hasattr(to_hist[par], 'compressed'):
            #logL = self.logL[skip:stop].compressed()
            #tohist = to_hist[par][skip:stop].compressed()
            _mask = to_hist[par].mask
            
            indices = np.arange(self.logL.size)
            
            if stop is None:
                stop = indices.size
            if skip is None:
                skip = 0
                
            _cond = np.logical_and(indices >= skip, indices <= stop)
            keep = np.logical_and(_cond, _mask == 0)
                        
            logL = self.logL[keep]
            tohist = to_hist[par][keep]
            
        else:
            logL = self.logL[skip:stop]
            tohist = to_hist[par][skip:stop]

        if logL.size != tohist.size:
            raise ValueError('logL and chain have different number of elements!')
            
        if peak == 'median':
            N = len(logL)
            psorted = np.sort(tohist)
            mu = psorted[int(N / 2.)]
        elif peak == 'mode':
            mu = tohist[np.argmax(logL)]
        else:
            mu = None
        
        if limit is None:
            q1 = 0.5 * 100 * (1. - nu)
            q2 = 100 * nu + q1
        elif limit == 'upper':
            q1 = 0.0
            q2 = 100 * nu 
        elif limit == 'lower':
            q1 = 100 * (1. - nu)
            q2 = 100
        else:
            raise ValueError('Unrecognized option for \'limit\': {!s}'.format(\
                limit))
                                
        # Do it already            
        lo, hi = np.percentile(tohist, (q1, q2))
                                
        if (mu is not None) and (limit is None):
            sigma = (mu - lo, hi - mu)
        else:
            sigma = (lo, hi)

        return mu, np.array(sigma)
        
    def _get_1d_kwargs(self, **kw):
        
        for key in ['labels', 'colors', 'linestyles']:
        
            if key in kw:
                kw.pop(key)

        return kw
        
    def _slice_by_nu(self, pars, z=None, take_log=False, bins=20, like=0.68,
        **constraints):
        """
        Return points in dataset satisfying given confidence contour.
        """
        
        binvec, to_hist = self._prep_plot(pars, z=z, bins=bins, 
            take_log=take_log)
        
        if not self.is_mcmc:
            self.set_constraint(**constraints)
        
        if not hasattr(self, '_weights'):
            weights = None
        else:
            weights = self.weights
        
        hist, xedges, yedges = \
            np.histogram2d(to_hist[0], to_hist[1], bins=binvec, 
            weights=weights)

        # Recover bin centers
        bc = []
        for i, edges in enumerate([xedges, yedges]):
            bc.append(rebin(edges))
                
        # Determine mapping between likelihood and confidence contours

        # Get likelihood contours (relative to peak) that enclose
        # nu-% of the area
        like, levels = self.get_levels(hist, nu=like)

        # Grab data within this contour.
        to_keep = np.zeros(to_hist[0].size)
        
        for i in range(hist.shape[0]):
            for j in range(hist.shape[1]):
                if hist[i,j] < levels[0]:
                    continue
                    
                # This point is good
                iok = np.logical_and(xedges[i] <= to_hist[0], 
                    to_hist[0] <= xedges[i+1])
                jok = np.logical_and(yedges[j] <= to_hist[1], 
                    to_hist[1] <= yedges[j+1])
                                    
                ok = iok * jok                    
                                                            
                to_keep[ok == 1] = 1
                
        model_set = ModelSubSet()
        model_set.chain = np.array(self.chain[to_keep == 1])
        model_set.base_kwargs = self.base_kwargs.copy()
        model_set.fails = []
        model_set.blobs = np.array(self.blobs[to_keep == 1,:,:])
        model_set.blob_names = self.blob_names
        model_set.blob_redshifts = self.blob_redshifts
        model_set.is_log = self.is_log
        model_set.parameters = self.parameters
        
        model_set.is_mcmc = self.is_mcmc
        
        if self.is_mcmc:
            model_set.logL = logL[to_keep == 1]
        else:
            model_set.axes = self.axes
        
        return ModelSet(model_set)
        
    def _prep_plot(self, pars, z=None, take_log=False, multiplier=1.,
        skip=0, skim=1, bins=20):
        """
        Given parameter names as strings, return data, bins, and log info.
        
        Returns
        -------
        Tuple : (bin vectors, data to histogram, is_log)
        """
        
        if type(pars) not in [list, tuple]:
            pars = [pars]
        if type(take_log) == bool:
            take_log = [take_log] * len(pars)
        if type(multiplier) in [int, float]:
            multiplier = [multiplier] * len(pars)    
        
        if type(z) is list:
            if len(z) != len(pars):
                raise ValueError('Length of z must be = length of pars!')
        else:
            z = [z] * len(pars)
        
        binvec = []
        to_hist = []
        is_log = []
        for k, par in enumerate(pars):

            if par in self.parameters:        
                j = self.parameters.index(par)
                is_log.append(self.is_log[j])
                
                val = self.chain[skip:,j].ravel()[::skim]
                                
                if self.is_log[j]:
                    val += np.log10(multiplier[k])
                else:
                    val *= multiplier[k]
                                
                if take_log[k] and not self.is_log[j]:
                    to_hist.append(np.log10(val))
                else:
                    to_hist.append(val)
                
            elif (par in self.blob_names) or (par in self.derived_blob_names):
                
                if z is None:
                    raise ValueError('Must supply redshift!')
                    
                i = self.blob_redshifts.index(z[k])
                
                if par in self.blob_names:
                    j = list(self.blob_names).index(par)
                else:
                    j = list(self.derived_blob_names).index(par)
                
                is_log.append(False)
                
                if par in self.blob_names:
                    val = self.blobs[skip:,i,j][::skim]
                else:
                    val = self.derived_blobs[skip:,i,j][::skim]
                
                if take_log[k]:
                    val += np.log10(multiplier[k])
                else:
                    val *= multiplier[k]
                
                if take_log[k]:
                    to_hist.append(np.log10(val))
                else:
                    to_hist.append(val)

            else:
                raise ValueError('Unrecognized parameter {!s}'.format(par))

            if not bins:
                continue
            
            # Set bins
            if self.is_mcmc or (par not in self.parameters):
                if type(bins) == int:
                    valc = to_hist[k]
                    binvec.append(np.linspace(valc.min(), valc.max(), bins))
                elif type(bins[k]) == int:
                    valc = to_hist[k]
                    binvec.append(np.linspace(valc.min(), valc.max(), bins[k]))
                else:
                    if take_log[k]:
                        binvec.append(np.log10(bins[k]))
                    else:
                        binvec.append(bins[k])
            else:
                if take_log[k]:
                    binvec.append(np.log10(self.axes[par]))
                else:
                    binvec.append(self.axes[par])
        
        return pars, to_hist, is_log, binvec
      
    def Limits(self, pars, ivar=None, take_log=False, un_log=False, 
        multiplier=1., remove_nas=False):
        
        data = self.ExtractData(pars, ivar=ivar, take_log=take_log,
            un_log=un_log, multiplier=multiplier, remove_nas=remove_nas)
            
        lims = {}
        for par in pars:
            lims[par] = (min(data[par]), max(data[par]))
            
        return lims
      
    def ExtractData(self, pars, ivar=None, take_log=False, un_log=False, 
        multiplier=1., remove_nas=False):
        """
        Extract data for subsequent analysis.

        This means a few things:
         (1) Go retrieve data from native format without having to worry about
          all the indexing yourself.
         (2) [optionally] take the logarithm.
         (3) [optionally] apply multiplicative factors.
         (4) Create a mask that excludes all nans / infs.

        Parameters
        ----------
        pars : list
            List of quantities to return. These can be parameters or the names
            of meta-data blobs.
        ivars : list
            List of independent variables at which to compute values of pars.
        take_log single bool or list of bools determining whether data should
                 be presented after its log is taken
        un_log single bool or list of bools determining whether data should be
               presented after its log is untaken (i.e. it is exponentiated)
        multiplier list of numbers to multiply the parameters by before they
                   are presented
        remove_nas bool determining whether rows with nan's or inf's should be
                   removed or not. This must be set to True when the user
                   is using numpy newer than version 1.9.x if the user wants
                   to histogram the data because numpy gave up support for
                   masked arrays in histograms.
        
        Returns
        -------
        Tuple with two entries:
         (i) Dictionary containing 1-D arrays of samples for each quantity.
         (ii) Dictionary telling us which of the datasets are actually the
          log10 values of the associated parameters.
         
        """

        pars, take_log, multiplier, un_log, ivar = \
            self._listify_common_inputs(pars, take_log, multiplier, un_log, 
            ivar)
                
        data = {}
        for k, par in enumerate(pars):
                                
            # If one of our free parameters, things are easy.
            if par in self.parameters:
                
                j = self.parameters.index(par)

                if self.is_log[j] and un_log[k]:
                    val = 10**self.chain[:,j].copy()
                else:
                    val = self.chain[:,j].copy()
                            
                if self.is_log[j] and (not un_log[k]):
                    val += np.log10(multiplier[k])
                else:
                    val *= multiplier[k]
                    
                # Take log, unless the parameter is already in log10
                if take_log[k] and (not self.is_log[j]):
                    val = np.log10(val)
               
            elif par == 'logL':
                val = self.logL
            elif par == 'load':
                val = self.load                        
                                        
            # Blobs are a little harder, might need new mask later.
            elif par in self.all_blob_names:
                
                i, j, nd, dims = self.blob_info(par)

                if nd == 0:
                    val = self.get_blob(par, ivar=None).copy()
                else:
                    val = self.get_blob(par, ivar=ivar[k]).copy()

                # Blobs are never stored as log10 of their true values         
                val *= multiplier[k]
                
            # Only derived blobs in this else block, yes?                        
            else:
                
                if re.search("\[", self.prefix):
                    print("WARNING: filenames with brackets can cause problems for glob.")
                    print("       : replacing each occurence with '?'")
                    _pre = self.prefix.replace('[', '?').replace(']', '?')
                else:
                    _pre = self.prefix
                
                cand = sorted(glob.glob('{0!s}.*.{1!s}.pkl'.format(_pre, par)))
                
                if len(cand) == 0:
                    cand =\
                        sorted(glob.glob('{0!s}*.{1!s}.pkl'.format(_pre, par)))
                
                if len(cand) == 0:
                    raise IOError('No results for {0!s}*.{1!s}.pkl'.format(\
                        self.prefix, par))
                # Only one option: go for it.
                elif len(cand) == 1:
                    fn = cand[0]
                elif len(cand) == 2:
                
                    # This, for example, could happen for files named after
                    # a parameter, like pop_fesc and pop_fesc_LW may get
                    # confused, or pop_yield and pop_yield_index.
                    pre1 = cand[0].partition('.')[0]
                    pre2 = cand[1].partition('.')[0]
                    
                    if pre1 in pre2:         
                        fn = cand[0]
                    else:
                        fn = cand[1]
                else:
                    print('{!s}'.format(cand))
                    raise IOError(('More than 2 options for ' +\
                        '{0!s}*{1!s}.pkl').format(self.prefix, par))
                
                dat = read_pickle_file(fn, nloads=1, verbose=False)
                
                # What follows is real cludgey...sorry, future Jordan
                nd = len(dat.shape) - 1
                dims = dat[0].shape
                #assert nd == 1, "Help!"
                
                # Need to figure out dimensions of derived blob,
                # which requires some care as that info will not simply
                # be stored in a binfo.pkl file.
                
                # Right now this may only work with 1-D blobs...
                if (nd == 2) and (ivar[k] is not None):
                    
                    fn_md = '{!s}.dbinfo.pkl'.format(self.prefix)
                    #dbinfo = {}
                    #dbinfos =\
                    #    read_pickle_file(fn_md, nloads=None, verbose=False)
                    #for info in dbinfos:
                    #    dbinfo.update(info)
                    #del dbinfos

                    # Look up the independent variables for this DB
                    #ivars = dbinfo[par]
                    ivars = self.derived_blob_ivars[par]

                    i1 = np.argmin(np.abs(ivars[0] - ivar[k][0]))
                    i2 = np.argmin(np.abs(ivars[1] - ivar[k][1]))
                    
                    #for iv in ivars:                            
                    #    arr = np.array(iv).squeeze()
                    #    if arr.shape == dat[0].shape:
                    #        break
                    #
                    #loc = np.argmin(np.abs(arr - ivar[k]))
                
                    val = dat[:,i1,i2]
                elif nd > 2:
                    raise NotImplementedError('help')
                else:
                    val = dat
                                
            # must handle log-ifying blobs separately
            if par not in self.parameters:
                if take_log[k]:
                    val = np.log10(val)
                                                          
            ##
            # OK, at this stage, 'val' is just an array. If it corresponds to
            # a parameter, it's 1-D, if a blob, it's dimensionality could
            # be different. So, we have to be a little careful with the mask.
            ##
              
            if par in self.parameters:
                j = self.parameters.index(par)
                if self.mask.ndim == 2:
                    mask = self.mask[:,j]
                else:
                    mask = self.mask
            elif not np.all(np.array(val.shape) == np.array(self.mask.shape)):
                
                # If no masked elements, don't worry any more. Just set -> 0.
                if not np.any(self.mask == 1):
                    mask = 0
                # Otherwise, we might need to reshape the mask.
                # If, for example, certain links in the MCMC chain are masked,
                # we need to make sure that every blob element corresponding
                # to those links are masked.
                else:
                    mask = np.zeros_like(val)
                    for j, element in enumerate(self.mask):
                        if np.all(element == 1):
                            mask[j].fill(1)
            else:
                mask = self.mask

            if self.is_mcmc:
                data[par] = np.ma.array(val, mask=mask)
            else:
                try:
                    data[par] = np.ma.array(val, mask=mask)
                except np.ma.MaskError:
                    print("MaskError encountered. Assuming mask=0.")
                    
                    data[par] = np.ma.array(val, mask=0)

        if remove_nas:
            to_remove = []
            length = len(data[list(data.keys())[0]])
            for ilink in range(length):
                for par in data:
                    elem = data[par][ilink]
                    if type(elem) is np.ma.core.MaskedConstant:
                        to_remove.append(ilink)
                        break
                    elif type(elem) in numerical_types:
                        if np.isinf(elem) or np.isnan(elem):
                            to_remove.append(ilink)
                            break
                    else: # elem is array (because par is a non-0d blob)
                        is_inf_or_nan = (np.isinf(elem) | np.isnan(elem))
                        if hasattr(elem, 'mask'): # ignore rows affected by mask
                            is_inf_or_nan = (is_inf_or_nan | elem.mask)
                        if not np.all(~is_inf_or_nan):
                            to_remove.append(ilink)
                            break
            for par in data:
                data[par] = np.delete(data[par], to_remove, axis=0)
            print(("{0} of {1} chain elements ignored because of chain " +\
                "links with inf's/nan's.").format(len(to_remove), length))

        return data

    def _set_bins(self, pars, to_hist, take_log=False, bins=20):
        """
        Create a vector of bins to be used when plotting PDFs.
        """
        
        if type(to_hist) is dict:
            binvec = {}
        else:
            binvec = []
            
        for k, par in enumerate(pars):
            
            if type(to_hist) is dict:
                tohist = to_hist[par]
            else:
                tohist = to_hist[k]
        
            if self.is_mcmc or (par not in self.parameters) or \
                not hasattr(self, 'axes'):
                if type(bins) == int:
                    valc = tohist
                    bvp = np.linspace(valc.min(), valc.max(), bins)
                elif type(bins[k]) == int:
                    valc = tohist
                    bvp = np.linspace(valc.min(), valc.max(), bins[k])
                else:
                    bvp = bins[k]
                    #if take_log[k]:
                    #    binvec.append(np.log10(bins[k]))
                    #else:
                    #    binvec.append(bins[k])
            else:
                if take_log[k]:
                    bvp = np.log10(self.axes[par])
                else:
                    bvp = self.axes[par]
        
            if type(to_hist) is dict:
                binvec[par] = bvp
            else:
                binvec.append(bvp)
        
        return binvec
        
    def _set_inputs(self, pars, inputs, take_log, un_log, multiplier):
        """
        Figure out input values for x and y parameters for each panel.
        
        Returns
        -------
        Dictionary, elements sorted by 
        """

        if inputs is None:
            return None
        
        if type(inputs) is list:
            if inputs == []:
                return None
        
        if type(inputs) is dict:
            if not inputs:
                return None
        else:
            inputs = list(inputs)
                        
        is_log = []
        for par in pars:
            if par in self.parameters:
                k = self.parameters.index(par)
                is_log.append(self.is_log[k])
            else:
                # Blobs are never log10-ified before storing to disk
                is_log.append(False)
        
        if type(multiplier) in [int, float]:
            multiplier = [multiplier] * len(pars)    
            
        if len(np.unique(pars)) < len(pars):
            input_output = []
        else:
            input_output = {}
        
        Nd = len(pars)
                                
        for i, par in enumerate(pars):
            if type(inputs) is list:
                val = inputs[i]
            elif par in inputs:
                val = inputs[par]
            else:
                dq = DQ(data=inputs)
                try:
                    val = dq[par]
                except:
                    val = None
                                                                                      
            # Take log [optional]    
            if val is None:
                vin = None
            elif (is_log[i] or take_log[i]) and (not un_log[i]):
                vin = np.log10(10**val * multiplier[i])                
            else:
                vin = val * multiplier[i]
                
            if type(input_output) is dict:
                input_output[par] = vin
            else:
                input_output.append(vin)
            
        return input_output
        
    def _listify_common_inputs(self, pars, take_log, multiplier, un_log, 
        ivar=None):
        """
        Make everything lists.
        """
        
        if type(pars) not in [list, tuple]:
            pars = [pars]
        if type(take_log) == bool:
            take_log = [take_log] * len(pars)
        if type(un_log) == bool:
            un_log = [un_log] * len(pars)    
        if type(multiplier) in [int, float]:
            multiplier = [multiplier] * len(pars)

        if ivar is not None:
            if type(ivar) is list:
                if len(pars) == 1:
                    i, j, nd, dims = self.blob_info(pars[0])
                    
                    if nd == 2:
                        ivar = list(np.atleast_2d(ivar))
                
                assert len(ivar) == len(pars)
            else:
                if len(pars) == 1:
                    ivar = [ivar]
                else:
                    raise ValueError('ivar must be same length as pars')    
                
        else:
            ivar = [None] * len(pars)
            
        return pars, take_log, multiplier, un_log, ivar

    def PosteriorCDF(self, pars, bins=500, **kwargs):
        return self.PosteriorPDF(pars, bins=bins, cdf=True, **kwargs)
               
    def PosteriorPDF(self, pars, to_hist=None, ivar=None, 
        ax=None, fig=1, 
        multiplier=1., like=[0.95, 0.68], cdf=False,
        color_by_like=False, fill=True, take_log=False, un_log=False,
        bins=20, skip=0, skim=1, 
        contour_method='raw', excluded=False, stop=None, **kwargs):
        """
        Compute posterior PDF for supplied parameters. 
    
        If len(pars) == 2, plot 2-D posterior PDFs. If len(pars) == 1, plot
        1-D marginalized PDF.
    
        Parameters
        ----------
        pars : str, list
            Name of parameter or list of parameters to analyze.
        ivar : float
            Redshift, if any element of pars is a "blob" quantity.
        plot : bool
            Plot PDF?
        nu : float, list
            If plot == False, return the nu-sigma error-bar.
            If color_by_like == True, list of confidence contours to plot.
        color_by_like : bool
            If True, color points based on what confidence contour they lie
            within.
        multiplier : list
            Two-element list of multiplicative factors to apply to elements of
            pars.
        take_log : list
            Two-element list saying whether to histogram the base-10 log of
            each parameter or not.
        skip : int
            Number of steps at beginning of chain to exclude. This is a nice
            way of doing a burn-in after the fact.
        skim : int
            Only take every skim'th step from the chain.
        excluded : bool
            If True, and fill == True, fill the area *beyond* the given contour with
            cross-hatching, rather than the area interior to it.

        Returns
        -------
        Either a matplotlib.Axes.axis object or a nu-sigma error-bar, 
        depending on whether we're doing a 2-D posterior PDF (former) or
        1-D marginalized posterior PDF (latter).
    
        """

        cs = None
        
        kw = kwargs

        if 'labels' in kw:
            labels = kwargs['labels']
        else:
            labels = self.custom_labels
            
        # Only make a new plot window if there isn't already one
        if ax is None:
            gotax = False
            fig = pl.figure(fig)
            ax = fig.add_subplot(111)
        else:
            gotax = True

        # Grab all the data we need
        if (to_hist is None):
            to_hist = self.ExtractData(pars, ivar=ivar, 
                take_log=take_log, un_log=un_log, multiplier=multiplier)

        pars, take_log, multiplier, un_log, ivar = \
            self._listify_common_inputs(pars, take_log, multiplier, un_log, 
            ivar)

        # Modify bins to account for log-taking, multipliers, etc.
        binvec = self._set_bins(pars, to_hist, take_log, bins)

        # We might supply weights by-hand for ModelGrid calculations
        if not hasattr(self, '_weights'):
            weights = None
        else:
            weights = self.weights

        ##
        ### Histogramming and plotting starts here
        ##

        if stop is not None:
            stop = -int(stop)
                    
        # Marginalized 1-D PDFs 
        if len(pars) == 1:
                        
            if type(to_hist) is dict:
                tohist = to_hist[pars[0]][skip:stop]
                b = binvec[pars[0]]
            elif type(to_hist) is list:
                tohist = to_hist[0][skip:stop]
                b = binvec[0]
            else:
                tohist = to_hist[skip:stop]
                b = bins
                                                
            if hasattr(tohist, 'compressed'):
                tohist = tohist.compressed()    
                                        
            hist, bin_edges = \
                np.histogram(tohist, density=True, bins=b, weights=weights)

            bc = rebin(bin_edges)

            # Take CDF
            if cdf:
                hist = np.cumsum(hist)

            tmp = self._get_1d_kwargs(**kw)
            
            ax.plot(bc, hist / hist.max(), drawstyle='steps-mid', **tmp)
            
            ax.set_ylim(0, 1.05)
            
        # Marginalized 2-D PDFs
        else:
            
            if type(to_hist) is dict:
                tohist1 = to_hist[pars[0]][skip:stop]
                tohist2 = to_hist[pars[1]][skip:stop]
                b = [binvec[pars[0]], binvec[pars[1]]]
            else:
                tohist1 = to_hist[0][skip:stop]
                tohist2 = to_hist[1][skip:stop]
                b = [binvec[0], binvec[1]]

            # If each quantity has a different set of masked elements,
            # we'll get an error at plot-time.
            if hasattr(tohist1, 'compressed'):
                tohist1 = tohist1.compressed()
            if hasattr(tohist2, 'compressed'):
                tohist2 = tohist2.compressed()    
             
            # Compute 2-D histogram
            hist, xedges, yedges = \
                np.histogram2d(tohist1, tohist2, bins=b, weights=weights)

            hist = hist.T

            # Recover bin centers
            bc = []
            for i, edges in enumerate([xedges, yedges]):
                bc.append(rebin(edges))

            # Determine mapping between likelihood and confidence contours
            if color_by_like:

                # Get likelihood contours (relative to peak) that enclose
                # nu-% of the area

                if contour_method == 'raw':
                    nu, levels = error_2D(None, None, hist, None, nu=like, 
                        method='raw')
                else:
                    nu, levels = error_2D(to_hist[0], to_hist[1], self.L / self.L.max(), 
                        bins=[binvec[0], binvec[1]], nu=nu, method=contour_method)
        
                if fill:
                    if excluded and len(nu) == 1:
                        # Fill the entire window with cross-hatching
                        x1, x2 = ax.get_xlim()
                        y1, y2 = ax.get_ylim()

                        x_polygon = [x1, x2, x2, x1]
                        y_polygon = [y1, y1, y2, y2]

                        ax.fill(x_polygon, y_polygon, color="none", hatch='X', 
                            edgecolor=kwargs['color'])
                            
                        # Now, fill the enclosed area with white
                        ax.contourf(bc[0], bc[1], hist / hist.max(), 
                            levels, color='w', colors='w', zorder=2)
                        # Draw an outline too   
                        ax.contour(bc[0], bc[1], hist / hist.max(), 
                            levels, colors=kwargs['color'], linewidths=1, 
                            zorder=2)
                        
                    else:
                        ax.contourf(bc[0], bc[1], hist / hist.max(), 
                            levels, zorder=3, **kwargs)
                    
                else:
                    ax.contour(bc[0], bc[1], hist / hist.max(),
                        levels, zorder=4, **kwargs)
                
            else:
                if fill:
                    cs = ax.contourf(bc[0], bc[1], hist / hist.max(), 
                        zorder=3, **kw)
                else:
                    cs = ax.contour(bc[0], bc[1], hist / hist.max(), 
                        zorder=4, **kw)

            # Force linear
            if not gotax:
                ax.set_xscale('linear')
                ax.set_yscale('linear')
                
            
        # Add nice labels (or try to)
        self.set_axis_labels(ax, pars, take_log, un_log, None, labels)

        # Rotate ticks?
        for tick in ax.get_xticklabels():
            tick.set_rotation(45.)
        for tick in ax.get_yticklabels():
            tick.set_rotation(45.)
        
        pl.draw()
        
        return ax
              
    def Contour(self, pars, c, levels=None, leveltol=1e-6, ivar=None, take_log=False,
        un_log=False, multiplier=1., ax=None, fig=1, fill=True, 
        inline_labels=False, manual=None, cax=None, use_colorbar=True, 
        cb_kwargs={}, **kwargs):         
        """
        Draw contours that are NOT associated with confidence levels.
        
        ..note:: To draw many contours in same plane, just call this 
            function repeatedly.
        
        Should use pl.contour if we're plotting on a regular grid, i.e.,
        the parameter space of a 2-D model grid with the color axis 
        some derived quantity.
        
        Parameters
        ----------
        pars : list 
            List of parameters defining the plane on which to draw contours.
        c : str
            Name of parameter or blob that we're to draw contours of.
        levels : list
            [Optional] list of levels for 
                        
        """
        
        # Only make a new plot window if there isn't already one
        if ax is None:
            gotax = False
            fig = pl.figure(fig)
            ax = fig.add_subplot(111)
        else:
            gotax = True
            
        cb = None    
        if (pars[0] in self.parameters) and (pars[1] in self.parameters):            
            xdata, ydata, zdata = self._reshape_data(pars, c, ivar=ivar, 
                take_log=take_log, un_log=un_log, multiplier=multiplier)
                                
            if fill:
                
                kw = kwargs.copy()
                kw.update(cb_kwargs)
                
                if levels is not None:
                    CS = ax.contourf(xdata, ydata, zdata.T, levels, **kw)
                else:
                    CS = ax.contourf(xdata, ydata, zdata.T, **kw)
                
                if use_colorbar:
                    cb = pl.colorbar(CS, cax=cax, **cb_kwargs)
            else:    
                if levels is not None:
                    CS = ax.contour(xdata, ydata, zdata.T, levels, **kwargs) 
                else:
                    CS = ax.contour(xdata, ydata, zdata.T, **kwargs) 
                    
                if inline_labels:
                    pl.clabel(CS, ineline=1, fontsize=10, manual=manual) 
                
        else:
            p = list(pars) + [c]

            # Grab all the data we need
            data = self.ExtractData(p, ivar=ivar, 
                take_log=take_log, un_log=un_log, multiplier=multiplier)
            
            xdata = data[p[0]]
            ydata = data[p[1]]    
            zdata = data[p[2]]
            
            for i, level in enumerate(levels):
                # Find indices of appropriate elements
                cond = np.abs(zdata - level) < leveltol
                elements = np.argwhere(cond).squeeze()
                
                order = np.argsort(xdata[elements])
                
                kw = {}
                for kwarg in kwargs.keys():
                    if type(kwargs[kwarg]) == tuple:
                        kw[kwarg] = kwargs[kwarg][i]
                    else:
                        kw[kwarg] = kwargs[kwarg]
                
                ax.plot(xdata[elements][order], ydata[elements][order], **kw)
                
        pl.draw()    
            
        return ax, cb

    def ContourScatter(self, x, y, c, z=None, Nscat=1e4, take_log=False, 
        cmap='jet', alpha=1.0, bins=20, vmin=None, vmax=None, zbins=None, 
        labels=None, **kwargs):
        """
        Show contour plot in 2-D plane, and add colored points for third axis.
        
        Parameters
        ----------
        x : str
            Fields for the x-axis.
        y : str
            Fields for the y-axis.
        c : str
            Name of parameter to represent with colored points.
        z : int, float, str
            Redshift (if investigating blobs)
        Nscat : int
            Number of samples plot.
            
        Returns
        -------
        Three objects: the main Axis instance, the scatter plot instance,
        and the colorbar object.
        
        """

        if type(take_log) == bool:
            take_log = [take_log] * 3

        if labels is None:
            labels = default_labels
        else:
            labels_tmp = default_labels.copy()
            labels_tmp.update(labels)
            labels = labels_tmp

        if type(z) is not list:
            z = [z] * 3

        pars = [x, y]

        axes = []
        for i, par in enumerate(pars):
            if par in self.parameters:
                axes.append(self.chain[:,self.parameters.index(par)])
            elif par in self.blob_names:
                axes.append(self.blobs[:,self.blob_redshifts.index(z[i]),
                    self.blob_names.index(par)])
            elif par in self.derived_blob_names:
                axes.append(self.derived_blobs[:,self.blob_redshifts.index(z[i]),
                    self.derived_blob_names.index(par)])        

        for i in range(2):
            if take_log[i]:
                axes[i] = np.log10(axes[i])

        xax, yax = axes

        if c in self.parameters:        
            zax = self.chain[:,self.parameters.index(c)].ravel()
        elif c in self.blob_names:   
            zax = self.blobs[:,self.blob_redshifts.index(z[-1]),
                self.blob_names.index(c)]
        elif c in self.derived_blob_names:   
            zax = self.derived_blobs[:,self.blob_redshifts.index(z[-1]),
                self.derived_blob_names.index(c)]
                
        if zax.shape[0] != self.chain.shape[0]:
            if self.chain.shape[0] > zax.shape[0]:
                xax = xax[0:self.blobs.shape[0]]
                yax = yax[0:self.blobs.shape[0]]
                print("Looks like calculation was terminated after chain " +\
                    "was written to disk but before blobs. How unlucky!")
                print("Applying cludge to ensure shape match...")
            else:
                raise ValueError('Shape mismatch between blobs and chain!')    
                
        if take_log[2]:
            zax = np.log10(zax)    
            
        z.pop(-1)
        ax = self.PosteriorPDF(pars, z=z, take_log=take_log, fill=False, 
            bins=bins, **kwargs)
        
        # Pick out Nscat random points to plot
        mask = np.zeros_like(xax, dtype=bool)
        rand = np.arange(len(xax))
        np.random.shuffle(rand)
        mask[rand < Nscat] = True
        
        if zbins is not None:
            cmap_obj = eval('mpl.colorbar.cm.{!s}'.format(cmap))
            #if take_log[2]:
            #    norm = mpl.colors.LogNorm(zbins, cmap_obj.N)
            #else:    
            if take_log[2]:
                norm = mpl.colors.BoundaryNorm(np.log10(zbins), cmap_obj.N)
            else:    
                norm = mpl.colors.BoundaryNorm(zbins, cmap_obj.N)
        else:
            norm = None
        
        scat = ax.scatter(xax[mask], yax[mask], c=zax[mask], cmap=cmap,
            zorder=1, edgecolors='none', alpha=alpha, vmin=vmin, vmax=vmax,
            norm=norm)
        cb = pl.colorbar(scat)

        cb.set_alpha(1)
        cb.draw_all()

        if c in labels:
            cblab = labels[c]
        elif '{' in c:
            cblab = labels[c[0:c.find('{')]]
        else:
            cblab = c 
            
        if take_log[2]:
            cb.set_label(logify_str(cblab))
        else:
            cb.set_label(cblab)    
            
        cb.update_ticks()
            
        pl.draw()
        
        return ax, scat, cb
        
    def ExtractPanel(self, panel, mp, ax=None, fig=99):
        """
        Save panel of a triangle plot as separate file.
        
        panel : int, str
            Integer or letter corresponding to plot panel you want.
        mp : MultiPlot instance
            Object representation of the triangle plot
        fig : int
            Figure number.
        
        """    
        
        letters = list(string.ascii_lowercase)
        letters.extend([let*2 for let in list(string.ascii_lowercase)])
        
        
        if isinstance(panel, basestring):
            panel = letters.index(panel)
        
        info = self.plot_info[panel]
        kw = self.plot_info['kwargs']
        
        ax = self.PosteriorPDF(info['axes'], z=info['z'], bins=info['bins'],
            multiplier=info['multiplier'], take_log=info['take_log'],
            fig=fig, ax=ax, **kw)
        
        ax.set_xticks(mp.grid[panel].get_xticks())
        ax.set_yticks(mp.grid[panel].get_yticks())
        
        xt = []
        for i, x in enumerate(mp.grid[panel].get_xticklabels()):
            xt.append(x.get_text())
        
        ax.set_xticklabels(xt, rotation=45.)
        
        yt = []
        for i, x in enumerate(mp.grid[panel].get_yticklabels()):
            yt.append(x.get_text())
            
        ax.set_yticklabels(yt, rotation=rotate_y)
        
        ax.set_xlim(mp.grid[panel].get_xlim())
        ax.set_ylim(mp.grid[panel].get_ylim())
        
        pl.draw()
        
        return ax
                
    def TrianglePlot(self, pars=None, ivar=None, take_log=False, un_log=False, 
        multiplier=1, fig=1, mp=None, inputs={}, tighten_up=0.0, ticks=5, 
        bins=20,  scatter=False, polygons=False, 
        skip=0, skim=1, stop=None, oned=True, twod=True, fill=True, 
        show_errors=False, label_panels=None, 
        fix=True, skip_panels=[], mp_kwargs={}, 
        **kwargs):
        """
        Make an NxN panel plot showing 1-D and 2-D posterior PDFs.

        Parameters
        ----------
        pars : list
            Parameters to include in triangle plot.
            1-D PDFs along diagonal will follow provided order of parameters
            from left to right. This list can contain the names of parameters,
            so long as the file prefix.pinfo.pkl exists, otherwise it should
            be the indices where the desired parameters live in the second
            dimension of the MCMC chain.

            NOTE: These can alternatively be the names of arbitrary meta-data
            blobs.

            If None, this will plot *all* parameters, so be careful!
        fig : int
            ID number for plot window.
        bins : int, np.ndarray
            Number of bins in each dimension. Or, array of bins to use
            for each parameter. If the latter, the bins should be in the 
            *final* units of the quantities of interest. For example, if
            you apply a multiplier or take_log, the bins should be in the
            native units times the multiplier or in the log10 of the native
            units (or both).
        ivar : int, float, str, list
            If plotting arbitrary meta-data blobs, must choose a redshift.
            Can be 'B', 'C', or 'D' to extract blobs at 21-cm turning points,
            or simply a number. If it's a list, it must have the same
            length as pars. This is how one can make a triangle plot 
            comparing the same quantities at different redshifts.
        input : dict
            Dictionary of parameter:value pairs representing the input
            values for all model parameters being fit. If supplied, lines
            will be drawn on each panel denoting these values.
        skip : int
            Number of steps at beginning of chain to exclude.
        stop: int
            Number of steps to exclude from the end of the chain.
        skim : int
            Only take every skim'th step from the chain.
        oned : bool    
            Include the 1-D marginalized PDFs?
        fill : bool
            Use filled contours? If False, will use open contours instead.
        color_by_like : bool
            If True, set contour levels by confidence regions enclosing nu-%
            of the likelihood. Set parameter `nu` to modify these levels.
        like : list
            List of levels, default is 1,2, and 3 sigma contours (i.e., 
            like=[0.68, 0.95])
        skip_panels : list
            List of panel numbers to skip over.
        polygons : bool
            If True, will just plot bounding polygons around samples rather
            than plotting the posterior PDF.
        mp_kwargs : dict 
            panel_size : list, tuple (2 elements)
                Multiplicative factor in (x, y) to be applied to the default 
                window size as defined in your matplotlibrc file. 
            
        ..note:: If you set take_log = True AND supply bins by hand, use the
            log10 values of the bins you want.
        
        Returns
        -------
        ares.analysis.MultiPlot.MultiPanel instance. Also saves a bunch of 
        information to the `plot_info` attribute.

        """    

        # Grab data that will be histogrammed
        np_version = np.__version__.split('.')
        newer_than_one = (int(np_version[0]) > 1)
        newer_than_one_pt_nine =\
            ((int(np_version[0]) == 1) and (int(np_version[1])>9))
        remove_nas = (newer_than_one or newer_than_one_pt_nine)
        
        to_hist = self.ExtractData(pars, ivar=ivar, take_log=take_log,
            un_log=un_log, multiplier=multiplier, remove_nas=remove_nas)
            
        # Make sure all inputs are lists of the same length!
        pars, take_log, multiplier, un_log, ivar = \
            self._listify_common_inputs(pars, take_log, multiplier, un_log, 
            ivar)        
            
        # Modify bins to account for log-taking, multipliers, etc.
        binvec = self._set_bins(pars, to_hist, take_log, bins)      
                            
        if type(binvec) is not list:
            bins = [binvec[par] for par in pars]      
        else:
            bins = binvec    
            
        if polygons:
            oned = False    
                 
        # Can opt to exclude 1-D panels along diagonal                
        if oned:
            Nd = len(pars)
        else:
            Nd = len(pars) - 1
                           
        # Setup MultiPanel instance
        had_mp = True
        if mp is None:
            had_mp = False
            
            mp_kw = default_mp_kwargs.copy()
            mp_kw['dims'] = [Nd] * 2    
            mp_kw.update(mp_kwargs)
            if 'keep_diagonal' in mp_kwargs:
                oned = False
            
            mp = MultiPanel(fig=fig, **mp_kw)
        
        # Apply multipliers etc. to inputs
        inputs = self._set_inputs(pars, inputs, take_log, un_log, multiplier)
                
        # Save some plot info for [optional] later tinkering
        self.plot_info = {}
        self.plot_info['kwargs'] = kwargs
        
        # Loop over parameters
        # p1 is the y-value, p2 is the x-value
        for i, p1 in enumerate(pars[-1::-1]):
            for j, p2 in enumerate(pars):

                # Row number is i
                # Column number is self.Nd-j-1

                if mp.diagonal == 'upper':
                    k = mp.axis_number(mp.N - i, mp.N - j)
                else:    
                    k = mp.axis_number(i, j)

                if k is None:
                    continue
                    
                if k in skip_panels:
                    continue

                if mp.grid[k] is None:
                    continue

                col, row = mp.axis_position(k)   
                
                # Read-in inputs values
                if inputs is not None:
                    if type(inputs) is dict:
                        xin = inputs[p2]
                        yin = inputs[p1]
                    else:
                        xin = inputs[j]
                        yin = inputs[-1::-1][i]
                else:
                    xin = yin = None
                    
                # 1-D PDFs on the diagonal    
                if k in mp.diag and oned:

                    # Grab array to be histogrammed
                    try:
                        tohist = [to_hist[j]]
                    except KeyError:
                        tohist = [to_hist[p2]]
                        
                    # Plot the PDF
                    ax = self.PosteriorPDF(p1, ax=mp.grid[k], 
                        to_hist=tohist,
                        take_log=take_log[-1::-1][i], ivar=ivar[-1::-1][i],
                        un_log=un_log[-1::-1][i], 
                        multiplier=[multiplier[-1::-1][i]], 
                        bins=[bins[-1::-1][i]], 
                        skip=skip, skim=skim, stop=stop, **kwargs)

                    # Stick this stuff in fix_ticks?
                    if col != 0:
                        mp.grid[k].set_ylabel('')
                    if row != 0:
                        mp.grid[k].set_xlabel('')

                    if show_errors:
                        mu, err = self.get_1d_error(p1, ivar=ivar[-1::-1][i])
                        mp.grid[k].plot([mu-err[0]]*2, [0, 1],
                            color='k', ls='--')
                        mp.grid[k].plot([mu+err[1]]*2, [0, 1],
                            color='k', ls='--')    
                        #mp.grid[k].set_title(err_str(p1, mu, err, 
                        #    self.is_log[i], labels), va='bottom', fontsize=18) 
                     
                    self.plot_info[k] = {}
                    self.plot_info[k]['axes'] = [p1]
                    self.plot_info[k]['data'] = tohist
                    self.plot_info[k]['ivar'] = ivar[-1::-1][i]
                    self.plot_info[k]['bins'] = [bins[-1::-1][i]]
                    self.plot_info[k]['multplier'] = [multiplier[-1::-1][i]]
                    self.plot_info[k]['take_log'] = take_log[-1::-1][i]
                                          
                    if not inputs:
                        continue
                        
                    self.plot_info[k]['input'] = xin
                        
                    if xin is not None:
                        mp.grid[k].plot([xin]*2, [0, 1.05], 
                            color='k', ls=':', lw=2, zorder=20)
                            
                    continue

                if ivar is not None:
                    iv = [ivar[j], ivar[-1::-1][i]]
                else:
                    iv = None

                # If not oned, may end up with some x vs. x plots if we're not careful
                if p1 == p2 and (iv[0] == iv[1]):
                    continue
                    
                try:
                    tohist = [to_hist[j], to_hist[-1::-1][i]]
                except KeyError:
                    tohist = [to_hist[p2], to_hist[p1]]
                                                    
                # 2-D PDFs elsewhere
                if scatter:
                    ax = self.Scatter([p2, p1], ax=mp.grid[k], 
                        take_log=[take_log[j], take_log[-1::-1][i]],
                        multiplier=[multiplier[j], multiplier[-1::-1][i]], 
                        skip=skip, stop=stop, **kwargs)
                elif polygons:       
                    ax = self.BoundingPolygon([p2, p1], ax=mp.grid[k], 
                        #to_hist=tohist,
                        take_log=[take_log[j], take_log[-1::-1][i]],
                        multiplier=[multiplier[j], multiplier[-1::-1][i]], 
                        fill=fill, 
                        skip=skip, stop=stop, **kwargs)
                else:
                    ax = self.PosteriorPDF([p2, p1], ax=mp.grid[k], 
                        to_hist=tohist, ivar=iv, 
                        take_log=[take_log[j], take_log[-1::-1][i]],
                        un_log=[un_log[j], un_log[-1::-1][i]],
                        multiplier=[multiplier[j], multiplier[-1::-1][i]], 
                        bins=[bins[j], bins[-1::-1][i]], fill=fill, 
                        skip=skip, stop=stop, **kwargs)

                if row != 0:
                    mp.grid[k].set_xlabel('')
                if col != 0:
                    mp.grid[k].set_ylabel('')
                    
                self.plot_info[k] = {}
                self.plot_info[k]['axes'] = [p2, p1]
                self.plot_info[k]['data'] = tohist
                self.plot_info[k]['ivar'] = iv
                self.plot_info[k]['bins'] = [bins[j], bins[-1::-1][i]]
                self.plot_info[k]['multiplier'] = [multiplier[j], multiplier[-1::-1][i]]
                self.plot_info[k]['take_log'] = [take_log[j], take_log[-1::-1][i]] 
                
                # Input values
                if not inputs:
                    continue
                                
                self.plot_info[k]['input'] = (xin, yin)

                mult = np.array([0.995, 1.005])

                # Plot as dotted lines
                if xin is not None:
                    mp.grid[k].plot([xin]*2, mult * np.array(mp.grid[k].get_ylim()), 
                        color='k',ls=':', zorder=20)
                if yin is not None:
                    mp.grid[k].plot(mult * np.array(mp.grid[k].get_xlim()), 
                        [yin]*2, color='k', ls=':', zorder=20)

        if oned:
            mp.grid[np.intersect1d(mp.left, mp.top)[0]].set_yticklabels([])

        if fix:
            mp.fix_ticks(oned=oned, N=ticks, rotate_x=45, rotate_y=45)

        if not had_mp:
            mp.rescale_axes(tighten_up=tighten_up)

        if label_panels is not None and (not had_mp):
            mp = self._label_panels(mp, label_panels)

        return mp

    def _label_panels(self, mp, label_panels):
        letters = list(string.ascii_lowercase)
        letters.extend([let*2 for let in list(string.ascii_lowercase)])
        
        ct = 0
        for ax in mp.grid:
            if ax is None:
                continue
        
            if label_panels == 'upper left':
                ax.annotate('({!s})'.format(letters[ct]), (0.05, 0.95),
                    xycoords='axes fraction', ha='left', va='top')
            elif label_panels == 'upper right':
                ax.annotate('({!s})'.format(letters[ct]), (0.95, 0.95),
                    xycoords='axes fraction', ha='right', va='top')
            elif label_panels == 'upper center':
                ax.annotate('({!s})'.format(letters[ct]), (0.5, 0.95),
                    xycoords='axes fraction', ha='center', va='top')
            elif label_panels == 'lower right':
                ax.annotate('({!s})'.format(letters[ct]), (0.95, 0.95),
                    xycoords='axes fraction', ha='right', va='top')
            else:
                print("WARNING: Uncrecognized label_panels option.")
                break
        
            ct += 1    
        
        pl.draw()    
        
        return mp
        
    def _reshape_data(self, pars, c, ivar=None, take_log=False,
        un_log=False, multiplier=1.):
        """
        Prepare datasets to make a contour plot.
        """
        
        assert len(pars) == 2
        assert pars[0] in self.parameters and pars[1] in self.parameters
        
        p = list(pars) + [c]        

        # Grab all the data we need
        data = self.ExtractData(p, ivar=ivar, 
            take_log=take_log, un_log=un_log, multiplier=multiplier)
            
        x = np.unique(data[pars[0]])
        y = np.unique(data[pars[1]])
        
        # Don't do this: grid may be incomplete!
        #assert x * y == data[c].size
        
        flat = data[c]
        zarr = np.inf * np.ones([len(x), len(y)])
        for i, xx in enumerate(x):
            for j, yy in enumerate(y):
                xok = xx == data[pars[0]]
                yok = yy == data[pars[1]]
                gotit = np.logical_and(xok, yok)
                
                if gotit.sum() == 0:
                    continue
                
                if type(gotit.sum()) == np.ma.core.MaskedConstant:
                    continue
                                
                k = np.argwhere(gotit == True)
                
                # If multiple elements, means this grid had redundant
                # elements. Shouldn't happen in the future!
                if len(k.shape) == 2:
                    # Just pick one
                    zarr[i,j] = flat[k].min()
                else:
                    zarr[i,j] = flat[k]
        
        return x, y, zarr
        
    def ReconstructedFunction(self, names, ivar=None, fig=1, ax=None,
        use_best=False, percentile=0.68, take_log=False, un_log=False, 
        multiplier=1, skip=0, stop=None, return_data=False, z_to_freq=False,
        best='mode', fill=True, samples=None, apply_dc=False, ivars=None,
        E_to_freq=False, **kwargs):
        """
        Reconstructed evolution in whatever the independent variable is.
        
        Parameters
        ----------
        names : str
            Name of quantity you're interested in.
        ivar : list, np.ndarray
            List of values (or nested list) of independent variables. If 
            blob is 2-D, only need to provide the independent variable for
            one of the dimensions, e.g.,

                # If LF data, plot LF at z=3.8
                ivar = [3.8, None]

            or 

                # If LF data, plot z evolution of phi(MUV=-20)
                ivar = [None, -20]
        
        ivars : np.ndarray
            If this is a derived blob, supply ivars by hand. Need to write
            automated way of figuring this out.
        
        percentile : bool, float    
            If not False, should be the confidence interval to plot, e.g, 0.68.
        use_best : bool
            If True, will plot the maximum likelihood reconstructed
            function. Otherwise, will use `percentile` and plot shaded region.
        samples : int, str
            If 'all', will plot all realizations individually. If an integer,
            will plot only that many realizations, drawn randomly.
 
        """

        if ax is None:
            gotax = False
            fig = pl.figure(fig)
            ax = fig.add_subplot(111)
        else:
            gotax = True
        
        if percentile:    
            q1 = 0.5 * 100 * (1. - percentile)    
            q2 = 100 * percentile + q1
            
        if isinstance(names, basestring):
            names = [names]

        max_samples = min(self.chain.shape[0], self.mask.size - self.mask.sum())
                
        if samples is not None:
            if type(samples) == int:
                samples = min(max_samples, samples)
                            
        # Step 1: figure out ivars  
        try:
            info = self.blob_info(names[0])
            nd = info[2]
        except KeyError:
            print("WARNING: blob {} not found by `blob_info`.".format(names[0]))
            print("       : Making some assumptions...")

            if ivars is None:
                ivars = self.get_ivars(names[0])
            else:
                if type(ivars) is str:
                    ivars = np.array(self.get_ivars(ivars))
                else:
                    ivars = np.atleast_2d(ivars)    

            nd = len(ivars)

        if ivars is None:    
            if nd == 1:
                # This first case happens when reading from hdf5 since the
                # blobs there aren't nested.
                if info[0] is None:
                    ivars = np.atleast_2d(self.blob_ivars[0])
                else:    
                    ivars = np.atleast_2d(self.blob_ivars[info[0]])
            else:
                assert len(names) == 1
                if names[0] in self.derived_blob_names:
                    ivars = self.derived_blob_ivars[names[0]]
                else:
                    ivars = self.blob_ivars[info[0]]

        if nd != 1 and (ivar is None):
            raise NotImplemented('If not 1-D blob, must supply one ivar!')
                
        ##
        # Real work starts here.
        ##
                        
        # First, read-in data from disk. Slice it up depending on if 
        # skip or stop were provided. Squeeze arrays to remove NaNs etc.
        
        # 1-D case. Don't need to specify ivar by hand.
        if nd == 1:
            
            # Read in the independent variable(s) and data itself
            xarr = ivars[0]     
                    
            if len(names) == 1:
                tmp = self.ExtractData(names[0], 
                    take_log=take_log, un_log=un_log, multiplier=multiplier)
                data = yblob = tmp[names[0]].squeeze()
            else:
                tmp = self.ExtractData(names, 
                    take_log=take_log, un_log=un_log, multiplier=multiplier)
                xblob = tmp[names[0]].squeeze()
                yblob = tmp[names[1]].squeeze()
                
                # In this case, xarr is 2-D. Need to be more careful...
                assert use_best
                                     
            # Only keep runs where ALL elements are OK.
            mask = np.all(yblob.mask == True, axis=1)
            keep = np.array(np.logical_not(mask), dtype=int)
            nans = np.any(np.isnan(yblob.data), axis=1)
                                         
            if skip is not None:
                keep[0:skip] *= 0
            if stop is not None:
                keep[stop: ] *= 0
            
            # Grab the maximum likelihood point
            if use_best and self.is_mcmc:
                if best == 'median':
                    N = len(self.logL[keep == 1])
                    psorted = np.argsort(self.logL[keep == 1])
                    loc = psorted[int(N / 2.)]
                else:
                    loc = np.argmax(self.logL[keep == 1])
                
                print('loc={}'.format(loc), keep.sum(), keep.size)
                
            # A few NaNs ruin everything
            if np.any(nans):
                print("WARNING: {} elements with NaNs detected in field={}. Will be discarded.".format(nans.sum(), names[0]))
                keep[nans == 1] = 0
                
            y = []
            for i, x in enumerate(xarr):
                
                # used to have compressed() here in a few places,
                # but it can mess up the shape for plotting...why
                # would certain channels get masked?
                                
                if (samples is not None):
                    y.append(yblob[keep == 1,i]) 
                elif (use_best and self.is_mcmc):
                    y.append(yblob[keep == 1,i][loc])
                elif percentile:
                    lo, hi = np.percentile(yblob[keep == 1,i], (q1, q2))
                    y.append((lo, hi))
                else:
                    dat = data[keep == 1,i]
                    lo, hi = dat.min(), dat.max()
                    y.append((lo, hi))

        elif nd == 2:
            if ivar[0] is None:
                scalar = ivar[1]
                vector = xarr = ivars[0]
                slc = slice(-1, None, -1)

            else:
                scalar = ivar[0]
                vector = xarr = ivars[1]
                slc = slice(0, None, 1)
            
            if type(multiplier) not in [list, np.ndarray, tuple]:
                multiplier = [multiplier] * len(vector)
                                                                      
            y = []
            for i, value in enumerate(vector):
                iv = [scalar, value][slc]
                              
                # Would be faster to pull this outside the loop                
                tmp = self.ExtractData(names, ivar=[iv]*len(names),
                    take_log=take_log, un_log=un_log, multiplier=[multiplier[i]])
                 
                if len(names) == 1:
                    yblob = tmp[names[0]]
                else:    
                    xblob = tmp[names[0]]
                    yblob = tmp[names[1]]

                #keep = np.ones_like(yblob.shape[0])

                mask = yblob.mask == True
                keep = np.array(np.logical_not(mask), dtype=int)
                nans = np.any(np.isnan(yblob.data)) 

                if skip is not None:
                    keep[0:skip] *= 0
                if stop is not None:
                    keep[stop: ] *= 0

                # Grab the maximum likelihood point
                if use_best and self.is_mcmc:
                    if best == 'median':
                        N = len(self.logL[keep == 1])
                        psorted = np.argsort(self.logL[keep == 1])
                        loc = psorted[int(N / 2.)]
                    else:
                        loc = np.argmax(self.logL[keep == 1])    
                
                if np.all(yblob[keep == 1].mask == 1):
                    print("WARNING: elements all masked!")
                    y.append(-np.inf)
                    continue

                if (use_best and self.is_mcmc):
                    #x.append(xblob[name][skip:stop][loc])        
                    y.append(yblob[loc]) 
                elif samples is not None:
                    y.append(yblob[keep == 1]) 
                elif percentile:
                    lo, hi = np.percentile(yblob[keep == 1], (q1, q2))
                    y.append((lo, hi))                    
                else:
                    dat = yblob[keep == 1]
                    lo, hi = dat.min(), dat.max()
                    y.append((lo, hi))

        # This assumes scalar is z!
        if apply_dc:
            xarr = self.dust.Mobs(scalar, xarr)

        y = np.array(y)
                        
        # At this stage, shape of y is (Nsamples, xarr)?

        # Convert redshifts to frequencies    
        if z_to_freq:
            xarr = nu_0_mhz / (1. + xarr)
            
        if E_to_freq:
            xarr = xarr * erg_per_ev / h_p

        ##
        # Do the actual plotting
        ##
                
        # Limit number of realizations
        if samples is not None:
            M = min(min(self.chain.shape[0], max_samples), len(y.T))            
            
            if samples == 'all':
                # Unmasked elements only
                elements = np.argwhere(self.mask == 0).squeeze()
                for i, element in enumerate(elements):
                    ax.plot(xarr, y.T[i], **kwargs)
            else:
                # Choose randomly 
                if type(samples) == int:    
                    elements = np.random.randint(0, M, size=samples)
                # Or take from list
                else:    
                    elements = samples
                
                for element in range(M):
                    if element not in elements:
                        continue
                        
                    ax.plot(xarr, y.T[element], **kwargs)
  
        elif use_best and self.is_mcmc:

            # Don't need to transpose in this case
            ax.plot(xarr, y, **kwargs)
        else:
        
            #if not take_log:
            #    # Where y is zero, set to small number?
            #    zeros = np.argwhere(y == 0)
            #    for element in zeros:
            #        y[element[0],element[1]] = 1e-15
            
            if fill:
                ax.fill_between(xarr, y.T[0], y.T[1], **kwargs)
            else:
                ax.plot(xarr, y.T[0], **kwargs)
                
                if 'label' in kwargs:
                    del kwargs['label']
                
                ax.plot(xarr, y.T[1], **kwargs)

        ax.set_ylabel(self.labeler.label(names[0]))

        pl.draw()

        if return_data:
            return ax, xarr, y
        else:
            return ax
        
    def ReconstructedRelation(self, names, ivar, xgrid, samples=None, **kwargs):
        """
        This is different from ReconstructedFunction because we're essentially
        plotting two reconstructed quantities against eachother.
        
        This just results in some gridding issues.
        
        Parameters
        ----------
        names : list, tuple
            
        
        """

        if ax is None:
            gotax = False
            fig = pl.figure(fig)
            ax = fig.add_subplot(111)
        else:
            gotax = True
        
        # Extract data
        data = self.ExtractData(names, ivar=ivar)
        
        # Pull out samples
        xs = data[names[0]]
        ys = data[names[1]]
        
        # Now, each sample will have a different array of x values, which is
        # where 'xgrid' comes into play
        
        
        
        """
        Should really hack out plotting piece of ReconstructedFunction, and
        make it so it accepts arrays of samples.
        """
        
        if samples is not None:
            if type(samples) is int:
                for i in range(samples):
                    ax.plot(xs[i], ys[i], **kwargs)
        
        return ax    
        
        
    def RedshiftEvolution(self, blob, ax=None, redshifts=None, fig=1,
        like=0.68, take_log=False, bins=20, label=None,
        plot_bands=False, limit=None, **kwargs):
        """
        Plot constraints on the redshift evolution of given quantity.

        Parameters
        ----------
        blob : str

        Note
        ----
        If you get a "ValueError: attempt to get argmin of an empty sequence"
        you might consider setting take_log=True.    

        """    

        if plot_bands and (limit is not None):
            raise ValueError('Choose bands or a limit, not both!')
        
        if ax is None:
            gotax = False
            fig = pl.figure(fig)
            ax = fig.add_subplot(111)
        else:
            gotax = True      
        
        try:
            ylabel = default_labels[blob]
        except KeyError:
            ylabel = blob
        
        if redshifts is None:
            redshifts = self.blob_redshifts
            
        if plot_bands or (limit is not None):
            x = []; ymin = []; ymax = []
            
        for i, z in enumerate(redshifts):
            
            # Skip turning points for banded plots
            if isinstance(z, basestring) and plot_bands:
                continue
            
            # Only plot label once
            if i == 0:
                l = label
            else:
                l = None
            
            try:
                value, (blob_err1, blob_err2) = \
                    self.get_1d_error(blob, ivar=z, like=like, take_log=take_log,
                    bins=bins, limit=limit)
            except TypeError:
                continue
            
            if value is None:
                continue    
            
            # Error on redshift
            if isinstance(z, basestring) and not plot_bands:
                if blob == 'dTb':
                    mu_z, (z_err1, z_err2) = \
                        self.get_1d_error('nu', ivar=z, nu=like, bins=bins)
                else:
                    mu_z, (z_err1, z_err2) = \
                        self.get_1d_error('z', ivar=z, nu=like, bins=bins)

                xerr = np.array(z_err1, z_err2).T
            else:
                mu_z = z
                xerr = None
            
            if plot_bands:
                if blob == 'dTb':
                    x.append(nu_0_mhz / (1. + mu_z))
                else:
                    x.append(z)
                ymin.append(value - blob_err1)
                ymax.append(value + blob_err2)
            elif limit is not None:
                if blob == 'dTb':
                    x.append(nu_0_mhz / (1. + mu_z))
                else:
                    x.append(z)
                ymin.append(value)
            else:                                    
                ax.errorbar(mu_z, value, 
                    xerr=xerr, 
                    yerr=np.array(blob_err1, blob_err2).T, 
                    lw=2, elinewidth=2, capsize=3, capthick=1, label=l,
                    **kwargs)        
        
        if plot_bands:
            ax.fill_between(x, ymin, ymax, **kwargs)
        elif limit is not None:
            ax.plot(x, ymin, **kwargs)
        
        # Look for populations
        m = re.search(r"\{([0-9])\}", blob)
        
        if m is None:
            prefix = blob
        else:
            # Population ID number
            num = int(m.group(1))
            
            # Pop ID excluding curly braces
            prefix = blob.split(m.group(0))[0]
        
        if blob == 'dTb':
            ax.set_xlabel(r'$\nu \ (\mathrm{MHz})$')
        else:
            ax.set_xlabel(r'$z$')
            
        ax.set_ylabel(ylabel)

        pl.draw()
        
        return ax
        
    def CovarianceMatrix(self, pars, ivar=None):
        """
        Compute covariance matrix for input parameters.

        Parameters
        ----------
        pars : list
            List of parameter names to include in covariance estimate.

        Returns
        -------
        Returns vector of mean, and the covariance matrix itself.
        
        """
        data = self.ExtractData(pars, ivar=ivar)
        
        blob_vec = []
        for i in range(len(pars)):
            blob_vec.append(data[pars[i]])    
        
        mu  = np.ma.mean(blob_vec, axis=1)
        cov = np.ma.cov(blob_vec)

        return mu, cov

    def PlotCovarianceMatrix(self, pars, ivar=None, fig=1, ax=None,\
        cmap='RdBu_r'):
        mu, cov = self.CovarianceMatrix(pars, ivar=ivar)
        if ax is None:
            fig = pl.figure(fig)
            ax = fig.add_subplot(111)

        cax = ax.imshow(cov, interpolation='none', cmap=cmap)
        cb = pl.colorbar(cax)

        return ax, cb
        
    def AssembleParametersList(self, N=None, ids=None, include_bkw=False, 
        **update_kwargs):
        """
        Return dictionaries of parameters corresponding to elements of the
        chain. Really just a convenience thing -- converting 1-D arrays 
        (i.e, links of the chain) into dictionaries -- so that the parameters
        can be passed into ares.simulations objects.
        
        .. note :: Masked chain elements are excluded.
        
        N : int
            Maximum number of models to return, starting from beginning of
            chain. If None, return all available.
        include_bkw : bool  
            Include base_kwargs? If so, then each element within the returned
            list can be supplied to an ares.simulations instance and recreate
            that model exactly.
        loc : int
            If supplied, only the dictionary of parameters associated with
            link `loc` in the chain will be returned.
        update_kwargs : dict
            New kwargs that you want added to each set of parameters. Will
            override pre-existing keys.
            
        Returns
        -------
        List of dictionaries. Maximum length: `N`.
            
        """ 
                
        ct = 0
        all_kwargs = []
        for i, element in enumerate(self.chain):
            
            if sum(self.mask[i]):
                continue
            
            if ids is not None:
                if type(ids) in [int, np.int64]:
                    if (i != ids):
                        continue
                else:    
                    if (i not in ids):
                        continue
            elif N is not None:
                if i >= N:
                    break

            if include_bkw:
                if ct == 0:
                    # Only print first time...could be thousands of iterations
                    print(("WARNING: Any un-pickleable kwargs will not " +\
                        "have been saved in {!s}.binfo.pkl!").format(\
                        self.prefix))
                kwargs = self.base_kwargs.copy()
            else:
                kwargs = {}

            for j, parameter in enumerate(self.parameters):
                if type(self.chain) == np.ma.core.MaskedArray:
                    if self.is_log[j]:
                        kwargs[parameter] = 10**self.chain.data[i,j]
                    else:
                        kwargs[parameter] = self.chain.data[i,j]
                else:
                    if self.is_log[j]:
                        kwargs[parameter] = 10**self.chain[i,j]
                    else:
                        kwargs[parameter] = self.chain[i,j]
                                        
            kwargs.update(update_kwargs)
            all_kwargs.append(kwargs.copy())
            
            ct += 1

        return all_kwargs

    def CorrelationMatrix(self, pars, ivar=None, fig=1, ax=None):
        """ Plot correlation matrix. """

        mu, cov = self.CovarianceMatrix(pars, ivar=ivar)

        corr = correlation_matrix(cov)

        if ax is None:
            fig = pl.figure(fig); ax = fig.add_subplot(111)

        cax = ax.imshow(corr, interpolation='none', cmap='RdBu_r', 
            vmin=-1, vmax=1)
        cb = pl.colorbar(cax)

        return ax
        
    def get_blob(self, name, ivar=None):
        """
        Extract an array of values for a given quantity.
        
        ..note:: If ivar is not supplied, this is equivalent to just reading
            all data from disk.
        
        Parameters
        ----------
        name : str
            Name of quantity
        ivar : list, tuple, array
            Independent variables a given blob may depend on.
            
        """
                        
        i, j, nd, dims = self.blob_info(name)
        
        if (i is None) and (j is None):
            f = h5py.File('{!s}.hdf5'.format(self.prefix), 'r')
            return f['blobs'][name].value
        
        blob = self.get_blob_from_disk(name)
                
        if nd == 0:
            return blob
        elif nd == 1:
            if ivar is None:
                return blob
            else:
                # Cludgey...
                biv = np.array(self.blob_ivars[i]).squeeze()
                k = np.argmin(np.abs(biv - ivar))
                
                if not np.allclose(biv[k], ivar):
                    print "WARNING: Looking for `{}` at ivar={}, closest found is {}.".format(name, ivar, biv[k])
                
                return blob[:,k]
        elif nd == 2:
            if ivar is None:
                return blob

            assert len(ivar) == 2, "Must supply 2-D coordinate for blob!"
            k1 = np.argmin(np.abs(self.blob_ivars[i][0] - ivar[0]))
            
            if not np.allclose(self.blob_ivars[i][0][k1], ivar[0]):
                print "WARNING: Looking for `{}` at ivar={}, closest found is {}.".format(name, 
                    ivar[0], self.blob_ivars[i][0][k1])
            
            
            if ivar[1] is None:
                return blob[:,k1,:]
            else:
                k2 = np.argmin(np.abs(self.blob_ivars[i][1] - ivar[1]))
                
                if self.blob_ivars[i][1][k2] != ivar[1]:
                    print "WARNING: Looking for `{}` at ivar={}, closest found is {}.".format(name, 
                        ivar[1], self.blob_ivars[i][1][k2])
                
                return blob[:,k1,k2]    
    
    def max_likelihood_parameters(self, method='mode', min_or_max='max'):
        """
        Return parameter values at maximum likelihood point.
        
        Parameters
        ----------
        method : str
            median or mode
            
        """
                    
        if method == 'median':
            N = len(self.logL)
            psorted = np.sort(self.logL)
            logL_med = psorted[int(N / 2.)]
            iML = np.argmin(np.abs(self.logL - logL_med))
        else:
            if min_or_max == 'max':
                iML = np.argmax(self.logL)
            else:
                iML = np.argmin(self.logL)
                
        print('iML={}'.format(iML))
                        
        self._max_like_pars = {}
        for i, par in enumerate(self.parameters):
            if self.is_log[i]:
                self._max_like_pars[par] = 10**self.chain[iML,i]
            else:
                self._max_like_pars[par] = self.chain[iML,i]
        
        return self._max_like_pars
        
    def DeriveBlob(self, func=None, fields=None, expr=None, varmap=None, 
        save=True, ivar=None, name=None, clobber=False):
        """
        Derive new blob from pre-existing ones.

        Parameters
        ----------
        Either supply the first two arguments:
        func : function!
            A function of two variables: ``data`` (a dictionary containing the 
            data) and ``ivars``, which contain the independent variables for
            each field in ``data``.
        fields : list, tuple
            List of quantities required by ``func``.
            
        OR the second two:    
            
        expr : str
            For example, 'x - y'
        varmap : dict
            Relates variables in `expr` to blobs. For example, 
            
            varmap = {'x': 'nu_D', 'y': 'nu_C'}

        The remaining parameters are:

        save : bool
            Save to disk? If not, just returns array.
        name : str
            If save==True, this is a name for this new blob that we can use
            to call it up later.
        clobber : bool
            If file with same ``name`` exists, overwrite it?

        """

        if func is not None:
            data = self.ExtractData(fields)
            
            # Grab ivars
            ivars_for_func = {}
            ivars = {}
            for key in data:
                # Don't need ivars if we're manipulating parameters!
                if key in self.parameters:
                    continue

                # Might be a derived blob of derived blobs!
                # Just err on the side of no ivars for now.  

                try: 
                    i, j, nd, size = self.blob_info(key)
                    
                    n = self.blob_ivarn[i]
                    
                    ivars[key] = self.blob_ivars[i]
                    
                    for k, _name in enumerate(n):
                        ivars_for_func[_name] = self.blob_ivars[i][k]
                        
                except KeyError:
                    ivars_for_func[key] = None
                    ivars[key] = None

            result = func(data, ivars_for_func)
        else:
            blobs = list(varmap.values())
            if ivar is not None:
                iv = [ivar[blob] for blob in blobs]
            else:
                iv = None

            data = self.ExtractData(blobs, ivar=iv)
            result = eval(expr, {var: data[varmap[var]] for var in varmap.keys()})
        
        if save:
            assert name is not None, "Must supply name for new blob!"
            
            # First dimension is # of samples
            nd = len(result.shape) - 1
            
            fn = '{0!s}.blob_{1}d.{2!s}.pkl'.format(self.prefix, nd, name)
            
            if os.path.exists(fn) and (not clobber):
                print(('{!s} exists! Set clobber=True or remove by ' +\
                    'hand.').format(fn))
                data = self.ExtractData(name)
                return data[name]
        
            write_pickle_file(result, fn, open_mode='w', ndumps=1,\
                safe_mode=False, verbose=False)
            
            # 'data' contains all field used to derive this blob.
            # Shape of new blob must be the same
            ivars = {}
            for key in data:
                # Don't need ivars if we're manipulating parameters!
                if key in self.parameters:
                    continue
                    
                try:    
                    i, j, nd, size = self.blob_info(key)
                    ivars[key] = self.blob_ivars[i]
                except KeyError:
                    ivars[key] = None
                    
            ##
            # Need to save ivars under new blob name.
            # Require ivars of component fields to be the same?
            ##
            
            ivars_f = {}
            if len(ivars.keys()) == 1:
                ivars_f[name] = ivars[ivars.keys()[0]]
            else:
                keys = ivars.keys()
                for k in range(1, len(keys)):
                    assert ivars[keys[k]] == ivars[keys[k-1]]
                
                ivars_f[name] = ivars[ivars.keys()[0]]
                         
            # Save metadata about this derived blob
            fn_md = '{!s}.dbinfo.pkl'.format(self.prefix)
            
            if (not os.path.exists(fn_md)) or clobber:
                write_pickle_file(ivars_f, fn_md, open_mode='w',\
                    ndumps=1, safe_mode=False, verbose=False)
            else:
                pdats = read_pickle_file(fn_md, nloads=None, verbose=False)
                for pdat in pdats:
                    if name in pdat:
                        if pdat[name] == ivars_f[name]:
                            break
                if pdat is not None:
                    write_pickle_file(ivars_f, fn_md, open_mode='a',\
                        ndumps=1, safe_mode=False, verbose=False)
        
        return result
        
    def z_to_freq(self, clobber=False):
        for tp in list('BCD'):
            self.DeriveBlob(expr='{:.5g} / (1. + x)'.format(nu_0_mhz),\
                varmap={'x': 'z_{!s}'.format(tp)}, name='nu_{!s}'.format(tp),\
                clobber=clobber)
            self.DeriveBlob(expr='{:.5g} / (1. + x)'.format(nu_0_mhz),\
                varmap={'x': 'z_{!s}p'.format(tp)},\
                name='nu_{!s}p'.format(tp), clobber=clobber)
                
    def RankModels(self, **kwargs):
        """
        Determine how close all models in ModelSet are to parameter set
        in kwargs.
        """
        
        # This is a list of all points in the chain represented as a 
        # dictionary of parameter:value pairs.
        all_kwargs = self.AssembleParametersList()
        
        scores = np.inf * np.ones(len(all_kwargs))
        
        for i, element in enumerate(all_kwargs):
            
            # Loop over parameters and add relative difference between
            # "reference model" parameter and that given

            for j, parameter in enumerate(self.parameters):
                if parameter not in element:
                    continue
                if parameter not in kwargs:
                    continue                                

                if element[parameter] is None:
                    continue
                if kwargs[parameter] is None:
                    continue

                if not np.isfinite(scores[i]):
                    scores[i] = 0

                score = abs(element[parameter] - kwargs[parameter]) \
                    / kwargs[parameter]
                scores[i] += score

        sorter = np.argsort(scores)    
        new_kw = [all_kwargs[i] for i in sorter]

        return sorter, new_kw, scores
        
    def export(self, pars, prefix=None, fn=None, ivar=None, path='.', 
        fmt='hdf5', clobber=False, skip=0, stop=None):
        """
        Just a wrapper around `save' routine.
        """
        self.save(pars, prefix=prefix, fn=fn, ivar=ivar, 
            path=path, fmt=fmt, clobber=clobber, skip=skip, stop=stop)
        
    def save(self, pars, prefix=None, fn=None, ivar=None, path='.', fmt='hdf5', 
        clobber=False, include_chain=True, restructure_grid=False,
        skip=0, stop=None):
        """
        Extract data from chain or blobs and output to separate file(s).
        
        This can be a convenient way to re-package data, for instance 
        consolidating data outputs from lots of processors into a single file,
        or simply reducing the size of a file for easy transport when we 
        don't need absolutely everything.
                
        Parameters
        ----------
        pars : str, list, tuple
            Name of parameter (or list of parameters) or blob(s) to extract.
        ivar : int, float, str, list, tuple
            [optional] independent variables, if None will extract all.
        fmt : str
            Options: 'hdf5' or 'pkl'
        path : str
            By default, will save files to CWD. Can modify this if you'd like.
        include_chain : bool
            By default, include the chain, which in the case of a ModelGrid,
            is just the axes of the grid.
        restructure_grid : bool
            Not implemented yet, but would be nice to restructure model grid
            data into an ordered mesh to be nice.
                
        """
        
        if type(pars) not in [list, tuple]:
            pars = [pars]
            
            for par in pars:
                if par in self.parameters:
                    print(("FYI: {!s} is a free parameter, so there's no " +\
                        "need to include it explicitly.").format(par))

        data = self.ExtractData(pars, ivar=ivar)
        
        if fn is None:
            assert prefix is not None
            fn =\
                '{0!s}/{1!s}.{2!s}.{3!s}'.format(path,self.prefix, prefix, fmt)
        
        if os.path.exists(fn) and (not clobber):
            raise IOError('File exists! Set clobber=True to wipe it.')
            
        # Output to HDF5. In this case, save each field as a new dataset
        if fmt == 'hdf5':
            
            assert have_h5py, "h5py import failed."
            
            f = h5py.File(fn, 'w')
            
            if include_chain:
                ds = f.create_dataset('chain', data=self.chain[skip:stop])
                ds.attrs.create('names', data=self.parameters)
                ds.attrs.create('is_log', data=self.is_log)
                f.create_dataset('mask', data=self.mask[skip:stop])
            else:
                # raise a warning? eh.
                pass

            # Loop over parameters and save to disk
            for par in pars:   
                
                # Tag ivars on as attribute if blob
                if 'blobs' not in f:
                    grp = f.create_group('blobs')
                else:
                    grp = f['blobs']
                
                dat = data[par][skip:stop]#[skip:stop:skim,Ellipsis]
                ds = grp.create_dataset(par, data=dat[self.mask[skip:stop] == 0])
                
                try:
                    i, j, nd, dims = self.blob_info(par)
                    
                    if self.blob_ivars[i] is not None:
                        # This might cause problems if the ivars are real big.
                        ds.attrs.create('ivar', self.blob_ivars[i])
                except KeyError:
                    print("Missing ivar info for {!s}!".format(par))    
                    
            f.close()
            print("Wrote {!s}.".format(fn))  
                        
        else:
            raise NotImplementedError('Only support for hdf5 so far. Sorry!')
            
        # Also make a copy of the info files with same prefix
        # since that's generally nice to have available.  
        # Well, it gives you a false sense of what data is available,
        # so sorry! Not doing that anymore.
        #out = '{0!s}/{1!s}.{2!s}.binfo.pkl'.format(path, self.prefix, prefix)
        #shutil.copy('{!s}.binfo.pkl'.format(self.prefix), out)
        #print "Wrote {!s}.".format(out)
        #
        #out = '{0!s}/{1!s}.{2!s}.pinfo.pkl'.format(path, self.prefix, prefix)
        #shutil.copy('{!s}.pinfo.pkl'.format(self.prefix), out)
        #print "Wrote {!s}.".format(out)
        
    @property
    def custom_labels(self):
        if not hasattr(self, '_custom_labels'):
            self._custom_labels = {}
        return self._custom_labels
    
    @custom_labels.setter
    def custom_labels(self, value):
    
        assert type(value) is dict

        if not hasattr(self, '_custom_labels'):
            self._custom_labels = {}
            
        for key in value:
            #if key not in self.parameters:
            #    print("WARNING: custom_label for par `{}` no in parameters list.".format(key))
        
            self._custom_labels[key] = value[key]
    
    
    @property
    def labeler(self):
        if not hasattr(self, '_labeler'):
            kw = self.base_kwargs if self.base_kwargs is not None else {}
            self._labeler = Labeler(self.parameters, self.is_log, 
                extra_labels=self.custom_labels, **kw)
        return self._labeler
        
    def set_axis_labels(self, ax, pars, take_log=False, un_log=False,
        cb=None, labels={}):
        """
        Make nice axis labels.
        """
                        
        pars, take_log, multiplier, un_log, ivar = \
            self._listify_common_inputs(pars, take_log, 1.0, un_log, None)

        is_log = {}
        for par in pars:
            if par in self.parameters:
                k = self.parameters.index(par)
                is_log[par] = self.is_log[k]
            else:
                # Blobs are never log10-ified before storing to disk
                is_log[par] = False

        if type(take_log) != dict:
            tmp = {par:take_log[i] for i, par in enumerate(pars)}
            take_log = tmp        
            
        # Prep for label making
        labeler = self.labeler #= Labeler(pars, is_log, extra_labels=labels,
            #**self.base_kwargs)

        # x-axis first
        ax.set_xlabel(labeler.label(pars[0], take_log=take_log[pars[0]], 
            un_log=un_log[0]))
    
        if len(pars) == 1:
            ax.set_ylabel('PDF')
            pl.draw()
            return
    
        ax.set_ylabel(labeler.label(pars[1], take_log=take_log[pars[1]], 
            un_log=un_log[1]))
            
        # Rotate ticks?
        for tick in ax.get_xticklabels():
            tick.set_rotation(45.)
        for tick in ax.get_yticklabels():
            tick.set_rotation(45.)
            
        # colorbar
        if cb is not None and len(pars) > 2:
            cb.set_label(labeler.label(pars[2], take_log=take_log[pars[2]], 
                un_log=un_log[2]))
        
        pl.draw()
        
        return ax

    def _alpha_shape(self, points, alpha):
        """
        
        Stolen from here:
        
        http://blog.thehumangeo.com/2014/05/12/drawing-boundaries-in-python/
        
        Thanks, stranger!
        
        Compute the alpha shape (concave hull) of a set
        of points.
        @param points: Iterable container of points.
        @param alpha: alpha value to influence the
            gooeyness of the border. Smaller numbers
            don't fall inward as much as larger numbers.
            Too large, and you lose everything!
            
        """
        
        if 1 <= len(points) < 4:
            # When you have a triangle, there is no sense
            # in computing an alpha shape.
            return geometry.MultiPoint(list(points)).convex_hull
        #else:
        #    return None, None
            
        def add_edge(edges, edge_points, coords, i, j):
            """
            Add a line between the i-th and j-th points,
            if not in the list already
            """
            if (i, j) in edges or (j, i) in edges:
                # already added
                return
            edges.add( (i, j) )
            edge_points.append(coords[ [i, j] ])
            
        coords = np.array(points)#np.array([point.coords[0] for point in points])
        tri = Delaunay(coords)
        edges = set()
        edge_points = []
        # loop over triangles:
        # ia, ib, ic = indices of corner points of the
        # triangle
        for ia, ib, ic in tri.vertices:
            pa = coords[ia]
            pb = coords[ib]
            pc = coords[ic]
            # Lengths of sides of triangle
            a = np.sqrt((pa[0]-pb[0])**2 + (pa[1]-pb[1])**2)
            b = np.sqrt((pb[0]-pc[0])**2 + (pb[1]-pc[1])**2)
            c = np.sqrt((pc[0]-pa[0])**2 + (pc[1]-pa[1])**2)
            # Semiperimeter of triangle
            s = (a + b + c)/2.0
            # Area of triangle by Heron's formula
            area = np.sqrt(s*(s-a)*(s-b)*(s-c))
            circum_r = a*b*c/(4.0*area)
            # Here's the radius filter.
            #print circum_r
            if circum_r < 1.0/alpha:
                add_edge(edges, edge_points, coords, ia, ib)
                add_edge(edges, edge_points, coords, ib, ic)
                add_edge(edges, edge_points, coords, ic, ia)
        m = geometry.MultiLineString(edge_points)
        triangles = list(polygonize(m))
        return cascaded_union(triangles), edge_points
    

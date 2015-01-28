"""

ModelGrid.py

Author: Jordan Mirocha
Affiliation: University of Colorado at Boulder
Created on: Thu Dec  5 15:49:16 MST 2013

Description: For working with big model grids. Setting them up, running them,
and analyzing them.

"""

import numpy as np
from ..simulations import Global21cm
import copy, os, pickle, gc, re, time
from ..util import GridND, ProgressBar
from ..util.ReadData import read_pickled_dataset, read_pickled_dict

try:
    from mpi4py import MPI
    rank = MPI.COMM_WORLD.rank
    size = MPI.COMM_WORLD.size
except ImportError:
    rank = 0
    size = 1

def_kwargs = {'track_extrema': True, 'verbose': False, 'progress_bar': False}    
    
class ModelGrid:
    """Create an object for setting up and running model grids."""
    def __init__(self, **kwargs):
        """
        Initialize a model grid.
        
        Parameters
        ----------

        prefix : str
            Will look for a file called <prefix>.grid.hdf5
        
        grid : instance

        verbose : bool

        """
        
        self.base_kwargs = def_kwargs.copy()
        self.base_kwargs.update(kwargs)
        
        # Prepare for blobs (optional)
        if 'inline_analysis' in self.base_kwargs:
            self.blob_names, self.blob_redshifts = \
                self.base_kwargs['inline_analysis']

    def __getitem__(self, name):
        return self.grid[name]
                        
    def set_blobs(self):
        pass                    
        
    def _read_restart(self, prefix):
        """
        Figure out which models have already been run.
        """

        fails = read_pickled_dict('%s.fail.pkl' % prefix)
        chain = read_pickled_dataset('%s.chain.pkl' % prefix)
        
        f = open('%s.grid.pkl' % prefix, 'rb')
        axes = pickle.load(f)
        self.base_kwargs = pickle.load(f)
        f.close()
        
        # Prepare for blobs (optional)
        if 'inline_analysis' in self.base_kwargs:
            self.blob_names, self.blob_redshifts = \
                self.base_kwargs['inline_analysis']
        
        self.set_axes(**axes)

        # Array of ones/zeros: has this model already been done?
        self.done = np.zeros(self.grid.shape)

        for link in chain:
            kw = {par : link[i] \
                for i, par in enumerate(self.grid.axes_names)}
            
            kvec = self.grid.locate_entry(kw)
            
            self.done[kvec] = 1
            
        for fail in fails:
            
            kvec = self.grid.locate_entry(fail)
            
            self.done[kvec] = 1

    def set_axes(self, **kwargs):
        """
        Create GridND instance, construct N-D parameter space.

        Parameters
        ----------

        """

        self.grid = GridND()

        if rank == 0:
            print "Building parameter space..."

        # Build parameter space
        self.grid.build(**kwargs)

        # Save for later access
        self.kwargs = kwargs

        # Shortcut to parameter names
        self.parameters = self.grid.axes_names

    @property
    def is_log(self):
        if not hasattr(self, '_is_log'):
            self._is_log = [False] * self.grid.Nd
        
        return self._is_log
        
    def prep_output_files(self, prefix, restart):
        """
        Stick this in utilities folder?
        """
        
        if rank != 0:
            return
        
        if restart:
            return
    
        # Main output: MCMC chains (flattened)
        f = open('%s.grid.pkl' % prefix, 'wb')
        pickle.dump(self.kwargs, f)
        pickle.dump(self.base_kwargs, f)
        f.close()
        
        if size > 1:
            f = open('%s.load.pkl' % prefix, 'wb')
            pickle.dump(self.assignments, f)
            f.close()
    
        # Main output: MCMC chains (flattened)
        f = open('%s.chain.pkl' % prefix, 'wb')
        f.close()
        
        # Failed models
        f = open('%s.fail.pkl' % prefix, 'wb')
        f.close()
        
        # Parameter names and list saying whether they are log10 or not
        f = open('%s.pinfo.pkl' % prefix, 'wb')
        pickle.dump((self.grid.axes_names, self.is_log), f)
        f.close()
        
        # Constant parameters being passed to ares.simulations.Global21cm
        f = open('%s.setup.pkl' % prefix, 'wb')
        tmp = self.base_kwargs.copy()
        to_axe = []
        for key in tmp:
            if re.search(key, 'tau_table'):
                to_axe.append(key)
        for key in to_axe:
            del tmp[key] # this might be big, get rid of it
        pickle.dump(tmp, f)
        del tmp
        f.close()

        if 'Tmin' in self.grid.axes_names:
            f = open('%s.fcoll.pkl' % prefix, 'wb')
            f.close()
        
        # Outputs for arbitrary meta-data blobs
        if hasattr(self, 'blob_names'):

            # File for blobs themselves
            f = open('%s.blobs.pkl' % prefix, 'wb')
            f.close()
            
            # Blob names and list of redshifts at which to track them
            f = open('%s.binfo.pkl' % prefix, 'wb')
            pickle.dump((self.blob_names, self.blob_redshifts), f)
            f.close()

    def run(self, prefix, clobber=False, restart=False, save_freq=10):
        """
        Run model grid, for each realization thru a given turning point.
        
        Parameters
        ----------
        prefix : str
            Prefix for all output files.
        save_freq : int
            Number of steps to take before writing data to disk.
        clobber : bool
            Overwrite pre-existing files of the same prefix if one exists?
        restart : bool
            Append to pre-existing files of the same prefix if one exists?

        Returns
        -------
        
        """
        
        if not hasattr(self, 'blob_names'):
            raise IOError('If you dont save anything this will be a useless exercise!')

        self.prefix = prefix

        if os.path.exists('%s.chain.pkl' % prefix) and (not clobber):
            if not restart:
                raise IOError('%s exists! Remove manually, set clobber=True, or set restart=True to append.' 
                    % prefix)

        if not os.path.exists('%s.chain.pkl' % prefix) and restart:
            raise IOError("This can't be a restart, %s*.pkl not found." % prefix)
        
        # Load previous results if this is a restart
        if restart:
            if rank != 0:
                MPI.COMM_WORLD.Recv(np.zeros(1), rank-1, tag=rank-1)
                
            self._read_restart(prefix)
            
            if rank != (size-1):
                MPI.COMM_WORLD.Send(np.zeros(1), rank+1, tag=rank)
        
        # Print out how many models we have (left) to compute
        if rank == 0:
            if restart:
                print "Update: %i models down, %i to go." \
                    % (self.done.sum(), self.grid.size - self.done.sum())
            else:
                print 'Running %i-element model-grid.' % self.grid.size
                         
        # Make some blank files for data output                 
        self.prep_output_files(prefix, restart)                 

        if not hasattr(self, 'LB'):
            self.LoadBalance(0)                    
                            
        # Dictionary for hmf tables
        fcoll = {}

        # Initialize progressbar
        pb = ProgressBar(self.grid.size, 'grid')
        pb.start()

        ct = 0    
        chain_all = []; blobs_all = []

        # Loop over models, use StellarPopulation.update routine 
        # to speed-up (don't have to re-load HMF spline as many times)
        for h, kwargs in enumerate(self.grid.all_kwargs):

            # Where does this model live in the grid?
            kvec = self.grid.locate_entry(kwargs)

            # Skip if it's a restart and we've already run this model
            if restart:
                if self.done[kvec]:
                    pb.update(h)
                    continue
                    
            # Skip if this processor isn't assigned to this model        
            if self.assignments[kvec] != rank:
                pb.update(h)
                continue
                
            # Grab Tmin index
            if self.Tmin_in_grid:
                Tmin_ax = self.grid.axes[self.grid.axisnum(self.Tmin_ax_name)]
                i_Tmin = Tmin_ax.locate(kwargs[self.Tmin_ax_name])
            else:
                i_Tmin = 0
                
            # Copy kwargs - may need updating with pre-existing lookup tables
            p = self.base_kwargs.copy()
            p.update(kwargs)

            # Create new splines if we haven't hit this Tmin yet in our model grid.
            if i_Tmin not in fcoll.keys():
                sim = Global21cm(**p)
                
                if hasattr(self, 'Tmin_ax_popid'):
                    loc = self.Tmin_ax_popid
                    suffix = '{%i}' % loc
                else:
                    loc = 0
                    suffix = ''
                                
                hmf_pars = {'Tmin%s' % suffix: sim.pf['Tmin%s' % suffix],
                    'fcoll%s' % suffix: copy.deepcopy(sim.pops.pops[loc].fcoll), 
                    'dfcolldz%s' % suffix: copy.deepcopy(sim.pops.pops[loc].dfcolldz)}

                # Save for future iterations
                fcoll[i_Tmin] = hmf_pars.copy()

            # If we already have matching fcoll splines, use them!
            else:        
                                        
                hmf_pars = {'Tmin%s' % suffix: fcoll[i_Tmin]['Tmin%s' % suffix],
                    'fcoll%s' % suffix: fcoll[i_Tmin]['fcoll%s' % suffix],
                    'dfcolldz%s' % suffix: fcoll[i_Tmin]['dfcolldz%s' % suffix]}
                p.update(hmf_pars)
                sim = Global21cm(**p)

            # Run simulation!
            try:
                sim.run()

                tps = sim.turning_points
            
            # Timestep error
            except SystemExit:
 
                sim.run_inline_analysis()
                tps = sim.turning_points
                
            except:         
                # Write to "fail" file - this might cause problems in parallel
                f = open('%s.fail.pkl' % self.prefix, 'ab')
                pickle.dump(kwargs, f)
                f.close()

                del p, sim
                gc.collect()

                pb.update(h)
                continue

            ct += 1
            
            chain = np.array([kwargs[key] for key in self.parameters])
            
            chain_all.append(chain)
            blobs_all.append(sim.blobs)

            del p, sim
            gc.collect()

            ##
            # File I/O from here on out
            ##
            
            pb.update(h)
            
            # Only record results every save_freq steps
            if ct % save_freq != 0:
                continue

            # Here we wait until we get the key
            if rank != 0:
                MPI.COMM_WORLD.Recv(np.zeros(1), rank-1, tag=rank-1)

            f = open('%s.chain.pkl' % self.prefix, 'ab')
            pickle.dump(chain_all, f)
            f.close()
            
            f = open('%s.blobs.pkl' % self.prefix, 'ab')
            pickle.dump(blobs_all, f)
            f.close()
            
            # Send the key to the next processor
            if rank != (size-1):
                MPI.COMM_WORLD.Send(np.zeros(1), rank+1, tag=rank)
            
            del chain_all, blobs_all
            gc.collect()

            chain_all = []; blobs_all = []

        pb.finish()

        # Need to make sure we write results to disk if we didn't 
        # hit the last checkpoint
        if rank != 0:
            MPI.COMM_WORLD.Recv(np.zeros(1), rank-1, tag=rank-1)
    
        if chain_all:
            f = open('%s.chain.pkl' % self.prefix, 'ab')
            pickle.dump(chain_all, f)
            f.close()
        
        if blobs_all:
            f = open('%s.blobs.pkl' % self.prefix, 'ab')
            pickle.dump(blobs_all, f)
            f.close()
        
        # Send the key to the next processor
        if rank != (size-1):
            MPI.COMM_WORLD.Send(np.zeros(1), rank+1, tag=rank)
        
        print "Processor %i done." % rank
        
    @property        
    def Tmin_in_grid(self):
        """
        Determine if Tmin is an axis in our model grid.
        """
        
        if not hasattr(self, '_Tmin_in_grid'):
        
            ct = 0
            name = None
            self._Tmin_in_grid = False
            for par in self.grid.axes_names:
                
                if par == 'Tmin':
                    ct += 1
                    self._Tmin_in_grid = True
                    name = par
                    continue
                
                if not re.search(par, 'Tmin'):
                    continue
                                
                # Look for populations
                m = re.search(r"\{([0-9])\}", par)
            
                if m is None:
                    continue
            
                # Population ID number
                num = int(m.group(1))
                self.Tmin_ax_popid = num
            
                # Pop ID including curly braces
                prefix = par.strip(m.group(0))
                
                if prefix == 'Tmin':
                    ct += 1
                    self._Tmin_in_grid = True
                    name = par
                    continue
                    
            self.Tmin_ax_name = name
            
            if ct > 1:
                raise NotImplemented('Trouble w/ multiple Tmin axes!')
                
        return self._Tmin_in_grid
            
    def LoadBalance(self, method=0):
        """
        Determine which processors are to run which models.
        
        Parameters
        ----------
        method : int
            0 : OFF
            1 : By Tmin, cleverly
            
        Returns
        -------
        Nothing. Creates "assignments" attribute, which has the same shape
        as the grid, with each element the rank of the processor assigned to
        that particular model.
        
        """
        
        self.LB = True
        
        if size == 1:
            self.assignments = np.zeros(self.grid.shape)
            return
            
        have_Tmin = self.Tmin_in_grid  
        
        if have_Tmin:
            Tmin_i = self.grid.axes_names.index(self.Tmin_ax_name)
            Tmin_ax = self.grid.axes[Tmin_i]
            Tmin_N = Tmin_ax.size  
        
        # No load balancing. Equal # of models per processor
        if method == 0 or (not have_Tmin) or (Tmin_N < size):
            
            k = 0
            tmp_assignments = np.zeros(self.grid.shape)
            for loc, value in np.ndenumerate(tmp_assignments):
                
                if k % size != rank:
                    k += 1
                    continue
                    
                tmp_assignments[loc] = rank    
            
                k += 1
            
            # Communicate results
            self.assignments = np.zeros(self.grid.shape)
            MPI.COMM_WORLD.Allreduce(tmp_assignments, self.assignments)

            self.LB = False        
            
        # Load balance over Tmin axis    
        elif method == 1:
            
            Tmin_slc = []
            
            for i in range(self.grid.Nd):
                if i == Tmin_i:
                    Tmin_slc.append(i)
                else:
                    Tmin_slc.append(Ellipsis)
            
            Tmin_slc = tuple(Tmin_slc)
            
            procs = np.arange(size)
                
            self.assignments = np.zeros(self.grid.shape)
            
            sequence = np.concatenate((procs, procs[-1::-1]))
            
            slc = [Ellipsis for i in range(self.grid.Nd)]
            
            k = 0
            for i in range(Tmin_N):
                
                slc[Tmin_i] = i
                
                self.assignments[slc] = k \
                    * np.ones_like(self.assignments[slc])
            
                k += 1
                if k == (len(sequence) / 2):
                    k = 0

        else:
            raise ValueError('No method=%i!' % method)


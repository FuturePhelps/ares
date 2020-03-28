"""

test_analysis_galaxy_pop.py

Author: Jordan Mirocha
Affiliation: McGill
Created on: Fri 27 Mar 2020 09:42:49 EDT

Description: 

"""

import ares

def test():
    
    gpop = ares.analysis.GalaxyPopulation()
        
    ax_lf = gpop.Plot(z=4, round_z=0.21, quantity='lf', fig=1)
    ax_smf = gpop.Plot(z=4, round_z=0.21, quantity='smf', fig=2)
    
    ax_lf_multi = gpop.MultiPlot([4,6,8], ncols=3, 
        round_z=0.21, quantity='lf', fig=3)
        
    #pars = ares.util.ParameterBundle('mirocha2017:base').pars_by_pop(0, 1)
    #pop = ares.populations.GalaxyPopulation(**pars)
    
    # Should run with a pop at some point
    ax_mega = gpop.MegaPlot(None, fig=4)    
    
    #gpop.PlotColors()
    #gpop.PlotColorEvolution()
    
    
if __name__ == '__main__':
    test()
    
    
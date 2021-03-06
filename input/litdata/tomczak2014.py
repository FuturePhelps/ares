"""
Tomczak et al., 2014, ApJ, 783, 85
"""

import numpy as np

info = \
{
 'reference':'Tomczak et al., 2014, ApJ, 783, 85',
 'data': 'Table 1', 
 'imf': ('chabrier', (0.1, 100.)),
}

redshifts = [2.25, 2.75]
wavelength = 1600.

ULIM = -1e10

fits = {}

# Table 1
tmp_data = {}
tmp_data['smf_tot'] = \
{
 #1.75: {'M': list(10**np.arange(9.25, 11.75, 0.25)),
 #     'phi': [-2.53, -2.50, -2.63, -2.74, -2.91, -3.07, -3.35, -3.54, -3.89, -4.41],
 #     'err': [(0.06, 0.07), (0.06, 0.07), (0.06, 0.07), (0.07, 0.08),
 #             (0.08, 0.09), (0.09, 0.10), (0.10, 0.13), (0.12, 0.16),
 #             (0.12, 0.17), (0.14, 0.19)],
 2.25: {'M': list(10**np.arange(9.25, 11.75, 0.25)),
     'phi': [-2.53, -2.50, -2.63, -2.74, -2.91, -3.07, -3.35, -3.54, -3.89, -4.41],
     'err': [(0.06, 0.07), (0.06, 0.07), (0.06, 0.07), (0.07, 0.08),
             (0.08, 0.09), (0.09, 0.10), (0.10, 0.13), (0.12, 0.16),
             (0.12, 0.17), (0.14, 0.19)],
    },
 2.75: {'M': list(10**np.arange(9.5, 11.75, 0.25)),
     'phi': [-2.65, -2.78, -3.02, -3.21, -3.35, -3.74, -4.00, -4.14, -4.73],
     'err': [(0.06, 0.07), (0.07, 0.08), (0.08, 0.09),
             (0.09, 0.10), (0.10, 0.13), (0.13, 0.17),
             (0.18, 0.25), (0.17, 0.28), (0.31, 2.00)],
    },            
}


units = {'smf_tot': 'log10', 'smf_sf': 'log10', 'smf': 'log10'}

tmp_data['smf_sf'] = \
{
 2.25: {'M': list(10**np.arange(9.25, 11.75, 0.25)),
     'phi': [-2.53, -2.51, -2.67, -2.78, -3.00, -3.26, -3.54, -3.69, -4.00, -4.59],
     'err': [(0.06, 0.07), (0.06, 0.07), (0.06, 0.07), (0.07, 0.08),
             (0.08, 0.09), (0.09, 0.11), (0.11, 0.14), (0.13, 0.17),
             (0.13, 0.17), (0.15, 0.21)],
    },
 2.75: {'M': list(10**np.arange(9.5, 11.75, 0.25)),
     'phi': [-2.66, -2.79, -3.06, -3.32, -3.59, -3.97, -4.16, -4.32, -4.94],
     'err': [(0.06, 0.07), (0.07, 0.08), (0.08, 0.09),
             (0.09, 0.11), (0.11, 0.14), (0.16, 0.20),
             (0.20, 0.28), (0.18, 0.29), (0.32, 2.00)],
    },            
}

data = {}
data['smf_tot'] = {}
data['smf_sf'] = {}
for group in ['smf_tot', 'smf_sf']:
    
    for key in tmp_data[group]:
        
        if key not in tmp_data[group]:
            continue
    
        subdata = tmp_data[group]
        
        mask = []
        for element in subdata[key]['err']:
            if element == ULIM:
                mask.append(1)
            else:
                mask.append(0)
        
        mask = np.array(mask)
        
        data[group][key] = {}
        data[group][key]['M'] = np.ma.array(subdata[key]['M'], mask=mask) 
        data[group][key]['phi'] = np.ma.array(subdata[key]['phi'], mask=mask) 
        data[group][key]['err'] = tmp_data['smf_sf'][key]['err']


data['smf'] = data['smf_sf']
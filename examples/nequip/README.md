# NequIP models in OpenMM-ML

This directory contains examples for running simulations using a NequIP potential.

## Installation


first install openmm-torch and pytorch from conda-forge:

```
conda install -c conda-forge openmm-torch pytorch=1.13
```

Then install NequIP development branch, this version of openmm-ml, and [torch-nl](https://github.com/felixmusil/torch_nl) using pip

```
pip install git+https://github.com/mir-group/nequip@develop
pip install git+https://github.com/sef43/openmm-ml@nequip
pip install torch-nl
```

## Usage

Once you have a deployed trained NequIP model you can use it at the potential in OpenMM-ML:

```python
from openmmml import MLPotential

# create a System with NequIP MLP

# need to specify the unit conversion factors from the NequIP model units to OpenMM units.
# e.g.:
# distance: model is in Angstrom, OpenMM is in nanometers
A_to_nm = 0.1
# energy: model is in kcal/mol, OpenMM is in kJ/mol
kcal_to_kJ_per_mol = 4.184

potential = MLPotential('nequip', model_path='example_model_deployed.pth',
                        distance_to_nm=A_to_nm,
                        energy_to_kJ_per_mol=kcal_to_kJ_per_mol)

system = potential.createSystem(topology)
```

## Examples
There are two examples in this folder than run example [NequIP](https://github.com/mir-group/nequip) models.


### run_nequip.ipynb
Runs a simulation using the model created by NequIP example [config/example.yaml](https://github.com/mir-group/nequip/blob/main/configs/example.yaml). It is available as a python script: [`run_nequip.py`](run_nequip.py) and a Jupyter notebook [`run_nequip.ipynb`](run_nequip.ipynb) which can be run on Colab.

### run_nequip_pbc.ipynb
Runs a simulation with PBCs using the using the model created by NequIP example [config/minimal_toy_emt.yaml](https://github.com/mir-group/nequip/blob/main/configs/minimal_toy_emt.yaml). It is available as a python script: [`run_nequip_pbc.py`](run_nequip.py) and a Jupyter notebook [`run_nequip_pbc.ipynb`](run_nequip.ipynb) which can be run on Colab.
"""
nequippotential.py: Implements the NequIP potential function.

This is part of the OpenMM molecular simulation toolkit originating from
Simbios, the NIH National Center for Physics-Based Simulation of
Biological Structures at Stanford, funded under the NIH Roadmap for
Medical Research, grant U54 GM072970. See https://simtk.org.

Portions copyright (c) 2021 Stanford University and the Authors.
Authors: Peter Eastman
Contributors: Stephen Farr

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
THE AUTHORS, CONTRIBUTORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

from openmmml.mlpotential import MLPotential, MLPotentialImpl, MLPotentialImplFactory
import openmm
from typing import Iterable, Optional, Union, Tuple
import torch
import torch.profiler



@torch.jit.script
def _simple_nl(positions: torch.Tensor, cell: torch.Tensor, pbc: bool, cutoff: float, sorti: bool=False) -> Tuple[torch.Tensor, torch.Tensor]:
    """simple torchscriptable neighborlist. 
    
    It aims are to be correct, clear, and torchscript compatible.
    It is O(n^2) but with pytorch vectorisation the prefactor is small.
    It outputs nieghbors and shifts in the same format as ASE:
    neighbors, shifts = simple_nl(..)
    is equivalent to
    
    [i, j], S = primitive_neighbor_list( quantities="ijS", ...)

    Limitations:
        - cell must be orthogonal
        - either no PBCs or PBCs in all x,y,z
        - cutoff must be less than half the smallest box length

    """

    num_atoms = positions.shape[0]
    device=positions.device

    # get i,j indices where j>i
    uij = torch.triu_indices(num_atoms, num_atoms, 1, device=device)
    triu_deltas = positions[uij[0]] - positions[uij[1]]

    cell_size = torch.diag(cell)

    if pbc:
        assert(cutoff < torch.min(cell_size)*0.5)

    wrapped_triu_deltas=triu_deltas.clone()

    if pbc:
        shifts = torch.round(triu_deltas/cell_size)
        wrapped_triu_deltas = triu_deltas - shifts*cell_size

    else:
        shifts = torch.zeros(triu_deltas.shape[0],3,device=device)


    triu_distances = torch.linalg.norm(wrapped_triu_deltas, dim=1)

    # filter
    mask = triu_distances > cutoff
    uij = uij[:,~mask]    
    wrapped_triu_deltas = wrapped_triu_deltas[~mask,:]

    shifts = shifts[~mask, :]

    # get the ij pairs where j<i
    lij = torch.stack((uij[1], uij[0]))
    neighbors = torch.hstack((uij, lij))
    shifts = torch.vstack((shifts, -shifts))

    if sorti:
        idx = torch.argsort(neighbors[0])
        neighbors = neighbors[:,idx]
        shifts = shifts[idx,:]

    return neighbors, shifts



class NequIPPotentialImplFactory(MLPotentialImplFactory):
    """This is the factory that creates NequipPotentialImpl objects."""

    def createImpl(self, name: str, model_path: str, distance_to_nm: float, energy_to_kJ_per_mol: float, atom_types: Optional[Iterable[int]]=None, **args) -> MLPotentialImpl:
        return NequIPPotentialImpl(name, model_path, distance_to_nm, energy_to_kJ_per_mol, atom_types)

class NequIPPotentialImpl(MLPotentialImpl):
    """This is the MLPotentialImpl implementing the NequIP potential.

    The potential is implemented using NequIP to build a PyTorch model.  A
    TorchForce is used to add it to the OpenMM System.  

    TorchForce requires the model to be saved to disk in a separate file.  By default
    it writes a file called 'nequipmodel.pt' in the current working directory.  You can
    use the filename argument to specify a different name.  For example,

    >>> system = potential.createSystem(topology, filename='mymodel.pt')
    """

    def __init__(self, name, model_path, distance_to_nm, energy_to_kJ_per_mol, atom_types):
        self.name = name
        self.model_path = model_path
        self.atom_types = atom_types
        self.distance_to_nm = distance_to_nm
        self.energy_to_kJ_per_mol = energy_to_kJ_per_mol

    def addForces(self,
                  topology: openmm.app.Topology,
                  system: openmm.System,
                  atoms: Optional[Iterable[int]],
                  forceGroup: int,
                  filename: str = 'nequipmodel.pt',
                  #implementation : str = None,
                  device: str = None,
                  **args):
        

        import torch
        import openmmtorch
        #from torch_nl import compute_neighborlist
        import nequip._version
        import nequip.scripts.deploy


        # Create the PyTorch model that will be invoked by OpenMM.

        includedAtoms = list(topology.atoms())
        if atoms is not None:
            #TODO: should atoms be sorted?
            includedAtoms = [includedAtoms[i] for i in atoms]
        

        class NequIPForce(torch.nn.Module):

            def __init__(self, model_path, includedAtoms, indices, periodic, distance_to_nm, energy_to_kJ_per_mol, atom_types=None, device=None, verbose=False):
                super(NequIPForce, self).__init__()

                if device is None: # use cuda if available
                    self.device=torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")

                else: # unless user has specified the device 
                    self.device=torch.device(device)

                
                # conversion constants 
                self.nm_to_distance = 1.0/distance_to_nm
                self.distance_to_nm = distance_to_nm
                self.energy_to_kJ = energy_to_kJ_per_mol

                
                
                self.model, metadata = nequip.scripts.deploy.load_deployed_model(model_path, device=self.device, freeze=False)

                

                self.default_dtype= {"float32": torch.float32, "float64": torch.float64}[metadata["model_dtype"]]
                torch.set_default_dtype(self.default_dtype)

                if verbose:
                    print(self.model)
                    print("running NequIPForce on device", self.device, "with dtype", self.default_dtype )
                    print("is periodic:", periodic)

                # instead load model directly using torch.jit.load and get the metadata we need
                #metadata = {k: "" for k in ["r_max","n_species","type_names"]}
                #self.model = torch.jit.load(model_path, _extra_files=metadata).to(device)
                #self.model.eval()
                # decode metadata
                #metadata = {k: v.decode("ascii") for k, v in metadata.items()}

                self.r_max = torch.tensor(float(metadata["r_max"]), device=self.device)
                
                if atom_types is not None: # use user set explicit atom types
                    # TODO: checks
                    nequip_types = atom_types
                
                else: # use openmm atomic symbols
                    # TODO: checks

                    type_names = str(metadata["type_names"]).split(" ")

                    type_name_to_type_index={ type_name : i for i,type_name in enumerate(type_names)}

                    nequip_types = [ type_name_to_type_index[atom.element.symbol] for atom in includedAtoms]
                
                atomic_numbers = [atom.element.atomic_number for atom in includedAtoms]

                self.atomic_numbers = torch.tensor(atomic_numbers,dtype=torch.long,device=self.device)
                self.N = len(includedAtoms)
                self.atom_types = torch.tensor(nequip_types,dtype=torch.long,device=self.device)

                if periodic:
                    self.pbc=torch.tensor([True, True, True], device=self.device)
                else:
                    self.pbc=torch.tensor([False, False, False], device=self.device)

                # indices for ML atoms in a mixed system
                if indices is None: # default all atoms are ML
                    self.indices = None
                else:
                    self.indices = torch.tensor(indices, dtype=torch.int64)


            def forward(self, positions, boxvectors: Optional[torch.Tensor] = None):
                # setup positions
                positions = positions.to(device=self.device, dtype=self.default_dtype)
                if self.indices is not None:
                    positions = positions[self.indices]
                positions = positions*self.nm_to_distance

                input_dict={}
                batch = torch.zeros(self.N,dtype=torch.long, device=self.device)

                if boxvectors is not None:
                    input_dict["cell"]=boxvectors.to(device=self.device, dtype=self.default_dtype) * self.nm_to_distance
                    pbc=True

                else:
                    input_dict["cell"]=torch.eye(3, device=self.device)
                    pbc=False

                self_interaction=False
                input_dict["pbc"]=self.pbc
                input_dict["atomic_numbers"] = self.atomic_numbers
                input_dict["atom_types"] = self.atom_types
                input_dict["pos"] = positions

                # compute edges

                # TODO: need to wrap coordinates to use torch_nl.compute_neighborlist (https://github.com/felixmusil/torch_nl/issues/1)
                #mapping, _ , shifts_idx = compute_neighborlist(cutoff=self.r_max, 
                #                                                            pos=input_dict["pos"], 
                #                                                            cell=input_dict["cell"], 
                #                                                            pbc=input_dict["pbc"], 
                #                                                            batch=batch, 
                #                                                            self_interaction=self_interaction)

                mapping, shifts_idx = _simple_nl(positions, input_dict["cell"], pbc, self.r_max)

                input_dict["edge_index"] = mapping
                input_dict["edge_cell_shift"] = shifts_idx

                out = self.model(input_dict)    

                # return energy and forces
                energy = out["total_energy"]*self.energy_to_kJ
                forces = out["forces"]*self.energy_to_kJ/self.distance_to_nm

                return (energy, forces)
            

        is_periodic = (topology.getPeriodicBoxVectors() is not None) or system.usesPeriodicBoundaryConditions()

        nequipforce = NequIPForce(self.model_path, includedAtoms, atoms, is_periodic, self.distance_to_nm, self.energy_to_kJ_per_mol, self.atom_types, device, **args)
        
        # Convert it to TorchScript and save it.
        module = torch.jit.script(nequipforce)
        module.save(filename)

        # Create the TorchForce and add it to the System.
        force = openmmtorch.TorchForce(filename)
        force.setForceGroup(forceGroup)
        force.setUsesPeriodicBoundaryConditions(is_periodic)
        force.setOutputsForces(True)
        system.addForce(force)

MLPotential.registerImplFactory('nequip', NequIPPotentialImplFactory())

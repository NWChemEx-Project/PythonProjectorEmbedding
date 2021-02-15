"""
Perform projector based embedding
"""
from pyscf import scf
from pyscf import dft
from pyscf import lo
from pyscf import mp
from pyscf import cc
from pyscf import df
import numpy as np
from scipy.linalg import fractional_matrix_power
from projectorEmbedding.embed_utils import make_dm
from projectorEmbedding.embed_utils import flatten_basis
from projectorEmbedding.embed_utils import purify
from projectorEmbedding.embed_utils import screen_aos
from projectorEmbedding.embed_utils import truncate_basis

def mulliken_partition(charge_threshold=0.4, localize=True):
    """splits the MOs into active and frozen parts based on charge threshold."""
    def internal(pyscf_mf, active_atoms=None, c_occ=None):
        offset_ao_by_atom = pyscf_mf.mol.offset_ao_by_atom()

        # if occupied coeffs aren't provided, get the ones from the mean field results.
        if c_occ is None:
            c_occ = pyscf_mf.mo_coeff[:, pyscf_mf.mo_occ > 0]
        overlap = pyscf_mf.get_ovlp()

        # localize orbitals
        if internal.localize:
            c_occ = lo.PM(pyscf_mf.mol, c_occ).kernel()

        # for each mo, go through active atoms and check the charge on that atom.
        # if charge on active atom is greater than threshold, mo added to active list.
        active_mos = []
        if active_atoms == []: # default case for NO active atoms
            return c_occ[:, []], c_occ[:, :]
        if active_atoms is None:
            return c_occ[:, :], c_occ[:, []]

        for mo_i in range(c_occ.shape[1]):

            rdm_mo = make_dm(c_occ[:, [mo_i]], pyscf_mf.mo_occ[mo_i])

            atoms = active_atoms

            for atom in atoms:
                offset = offset_ao_by_atom[atom, 2]
                extent = offset_ao_by_atom[atom, 3]

                overlap_atom = overlap[:, offset:extent]
                rdm_mo_atom = rdm_mo[:, offset:extent]

                q_atom_mo = np.einsum('ij,ij->', rdm_mo_atom, overlap_atom)

                if q_atom_mo > internal.charge_threshold:
                    active_mos.append(mo_i)
                    break

        # all mos not active are frozen
        frozen_mos = [i for i in range(c_occ.shape[1]) if i not in active_mos]

        return c_occ[:, active_mos], c_occ[:, frozen_mos]

    internal.charge_threshold = charge_threshold
    internal.localize = localize

    return internal

def occupancy_partition(occupancy_threshold=0.4, localize=True):
    """splits the MOs into active and frozen parts based on occupancy threshold."""
    def internal(pyscf_mf, active_atoms=None, c_occ=None):
        # Handle orbital coefficients
        if c_occ is None:
            c_occ = pyscf_mf.mo_coeff[:, pyscf_mf.mo_occ > 0]
        if internal.localize:
            c_occ = lo.PM(pyscf_mf.mol, c_occ).kernel()
        overlap = pyscf_mf.get_ovlp()

        # Handle active atoms
        if active_atoms == []: # default case for NO active atoms
            return c_occ[:, []], c_occ[:, :]
        if active_atoms is None:
            return c_occ[:, :], c_occ[:, []]

        # Find AOs on active atoms
        offset_ao_by_atom = pyscf_mf.mol.offset_ao_by_atom()
        active_aos = []
        for atom in active_atoms:
            active_aos += list(range(offset_ao_by_atom[atom, 2], offset_ao_by_atom[atom, 3]))
        mesh = np.ix_(active_aos, active_aos)

        # Find MO occupancies in active AOs and sort accordingly
        active_mos = []
        frozen_mos = []
        for mo_i in range(c_occ.shape[1]):
            rdm_mo = make_dm(c_occ[:, [mo_i]], 1)
            dm_mo = rdm_mo @ overlap
            if np.trace(dm_mo[mesh]) > internal.occupancy_threshold:
                active_mos.append(mo_i)
            else:
                frozen_mos.append(mo_i)

        return c_occ[:, active_mos], c_occ[:, frozen_mos]

    internal.occupancy_threshold = occupancy_threshold
    internal.localize = localize

    return internal

def spade_partition(pyscf_mf, active_atoms=None, c_occ=None):
    """SPADE partitioning scheme"""

    # things coming from molecule.
    offset_ao_by_atom = pyscf_mf.mol.offset_ao_by_atom()

    # things coming from mean field calculation.
    mo_occ = pyscf_mf.mo_occ
    if c_occ is None:
        c_occ = pyscf_mf.mo_coeff[:, mo_occ > 0]
    overlap = pyscf_mf.get_ovlp()

    active_aos = []
    for atom in active_atoms:
        active_aos += list(range(offset_ao_by_atom[atom, 2], offset_ao_by_atom[atom, 3]))

    overlap_sqrt = fractional_matrix_power(overlap, 0.5)
    c_orthogonal_ao = (overlap_sqrt @ c_occ)[active_aos, :]
    _, s_vals, v_vecs = np.linalg.svd(c_orthogonal_ao, full_matrices=True)

    if len(s_vals) == 1:
        n_act_mos = 1
    else:
        if len(s_vals) != v_vecs.shape[0]:
            s_vals = np.append(s_vals, [0.0])
        deltas = [-(s_vals[i + 1] - s_vals[i]) for i in range(len(s_vals)-1)]
        n_act_mos = np.argpartition(deltas, -1)[-1]+1

    c_a = c_occ @ v_vecs.T[:, :n_act_mos]
    c_b = c_occ @ v_vecs.T[:, n_act_mos:]

    return c_a, c_b

def embedding_procedure(init_mf, active_atoms=None, embed_meth=None,
                        mu_val=10**6, trunc_lambda=None,
                        distribute_mos=mulliken_partition()):
    """Manby-like embedding procedure."""
    # initial information
    mol = init_mf.mol.copy()
    ovlp = init_mf.get_ovlp()
    c_occ = init_mf.mo_coeff[:, init_mf.mo_occ > 0]

    # get active mos
    c_occ_a, _ = distribute_mos(init_mf, active_atoms=active_atoms, c_occ=c_occ)

    # make full and subsystem densities
    dens = {}
    dens['ab'] = make_dm(c_occ, init_mf.mo_occ[init_mf.mo_occ > 0])
    dens['a'] = make_dm(c_occ_a, init_mf.mo_occ[:c_occ_a.shape[1]])
    dens['b'] = dens['ab'] - dens['a']

    # build embedding potential
    f_ab = init_mf.get_fock()
    v_a = init_mf.get_veff(dm=dens['a'])

    hcore_a_in_b = f_ab - v_a
    if mu_val is None:
        hcore_a_in_b -= 0.5 * (f_ab @ dens['b'] @ ovlp + ovlp @ dens['b'] @ f_ab)
    else:
        hcore_a_in_b += mu_val * (ovlp @ dens['b'] @ ovlp)

    # get electronic energy for A
    energy_a, _ = init_mf.energy_elec(dm=dens['a'], vhf=v_a, h1e=hcore_a_in_b)

    # set new number of electrons
    mol.nelectron = int(sum(init_mf.mo_occ[:c_occ_a.shape[1]]))

    if trunc_lambda:
        print('Truncating AO Space')

        # alter basis set to facilitate screening
        print(' Flattening Basis Set')
        mol.build(basis=flatten_basis(mol))

        # screen basis sets for truncation
        active_aos, include = screen_aos(mol, active_atoms, dens['a'], ovlp, trunc_lambda)
        print("Active AOs:", len(active_aos), "/", mol.nao)

        if len(active_aos) != mol.nao:
            # make truncated basis set
            mol.build(dump_input=True, basis=truncate_basis(mol, include))

            # make appropiate mean field object with new molecule
            if hasattr(init_mf, 'xc'):
                tinit_mf = dft.RKS(mol)
                tinit_mf.xc = init_mf.xc
            else:
                tinit_mf = scf.RHF(mol)
            if hasattr(init_mf, 'with_df'):
                tinit_mf = df.density_fit(tinit_mf)
                tinit_mf.with_df.auxbasis = init_mf.with_df.auxbasis

            # make truncated tensors
            mesh = np.ix_(active_aos, active_aos)
            hcore_a_in_b = hcore_a_in_b[mesh]
            pure_d_a = 2 * purify(dens['a'][mesh] / 2, ovlp[mesh])

            # truncated initial method (self embedded)
            tinit_mf.get_hcore = lambda *args: hcore_a_in_b
            tinit_mf.kernel(pure_d_a)

            # overwrite previous values
            dens['a'] = tinit_mf.make_rdm1()
            v_a = tinit_mf.get_veff(dm=dens['a'])
            energy_a, _ = tinit_mf.energy_elec(dm=dens['a'], vhf=v_a, h1e=hcore_a_in_b)

    # make embedding mean field object
    if embed_meth.lower() in ['rhf', 'mp2', 'ccsd', 'ccsd(t)']:
        mf_embed = scf.RHF(mol)
    else: # assume anything else is a functional name
        mf_embed = dft.RKS(mol)
        mf_embed.xc = embed_meth
    if hasattr(init_mf, 'with_df'):
        mf_embed = df.density_fit(mf_embed)
        mf_embed.with_df.auxbasis = init_mf.with_df.auxbasis
    mf_embed.get_hcore = lambda *args: hcore_a_in_b

    # run embedded SCF
    tot_energy_a_in_b = mf_embed.kernel(dens['a'])

    # get electronic energy for embedded part
    energy_a_in_b = tot_energy_a_in_b - mf_embed.energy_nuc()

    # recombined energy with embedded part
    results = (init_mf.e_tot - energy_a + energy_a_in_b, )

    # correlated WF method
    if embed_meth.lower() == 'mp2':
        embed_corr = mp.MP2(mf_embed)
        embed_corr.kernel()
        results = results + (embed_corr.e_corr,)

    elif embed_meth.lower() in ['ccsd', 'ccsd(t)']:
        embed_corr = cc.CCSD(mf_embed)
        embed_corr.kernel()
        results = results + (embed_corr.emp2,)
        results = results + (embed_corr.e_corr - embed_corr.emp2,)
        if embed_meth.lower() == 'ccsd(t)':
            results = results + (embed_corr.ccsd_t(),)

    return results

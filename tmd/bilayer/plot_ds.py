import os
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.cm as cm
from tmd.pwscf.parseScf import total_energy_eV_from_scf
from tmd.wannier.parseWout import atom_order_from_wout
from tmd.wannier.bands import Hk_recip
from tmd.bilayer.dfourier import get_Hr
from tmd.bilayer.bilayer_util import global_config
from tmd.bilayer.dgrid import get_prefixes

def ds_from_prefixes(prefixes):
    ds = []
    for prefix in prefixes:
        sp = prefix.split("_")
        da = float(sp[-3])
        db = float(sp[-1])
        ds.append((da, db))

    return ds

def wrap_cell(ds, values):
    wrapped_ds, wrapped_values = [], []
    for d, v in zip(ds, values):
        da, db = d[0], d[1]
        wrapped_ds.append(d)
        wrapped_values.append(v)

        if da == 0.0 and db == 0.0:
            wrapped_ds.append((1.0, 1.0))
            wrapped_values.append(v)
            wrapped_ds.append((0.0, 1.0))
            wrapped_values.append(v)
            wrapped_ds.append((1.0, 0.0))
            wrapped_values.append(v)
        elif da == 0.0:
            wrapped_ds.append((1.0, db))
            wrapped_values.append(v)
        elif db == 0.0:
            wrapped_ds.append((da, 1.0))
            wrapped_values.append(v)

    return wrapped_ds, wrapped_values

def sorted_d_group(ds, values):
    '''Zip ds with values and sort such that d's (and their associated values)
    are in ascending order, with db ascending faster than da.
    '''
    dvs = list(zip(ds, values))
    dvs = sorted(dvs, key=lambda dp: dp[0][1])
    dvs = sorted(dvs, key=lambda dp: dp[0][0])
    return dvs

def get_energies(work, dps):
    energies = []
    for d, prefix in dps:
        wannier_dir = os.path.join(work, prefix, "wannier")
        scf_path = os.path.join(wannier_dir, "scf.out")
        energy = total_energy_eV_from_scf(scf_path)
        energies.append(energy)

    return energies

def energies_relative_to(energies, dps, base_d):
    base_d_index = None
    for d_i, (d, prefix) in enumerate(dps):
        if d == (0.0, 0.0):
            base_d_index = d_i

    if base_d_index is None:
        raise ValueError("d = (0, 0) not found")

    base_energy = energies[base_d_index]
    energies_rel_meV = []
    for E in energies:
        E_rel = (E - base_energy) * 1000
        energies_rel_meV.append(E_rel)

    return energies_rel_meV

def sort_order(xs, f):
    with_orig_order = zip(xs, range(len(xs)))
    def wrap_f(x):
        return f(x[0])

    xs_sorted = sorted(with_orig_order, key=wrap_f)
    order = []
    for x, orig_index in xs_sorted:
        order.append(orig_index)

    return order

def get_atom_order(work, prefix):
    z_order_syms = ["X1", "M", "X2", "X1p", "Mp", "X2p"]
    wannier_dir = os.path.join(work, prefix, "wannier")
    wout_path = os.path.join(wannier_dir, "{}.wout".format(prefix))

    atom_symbols, atom_indices, cart_coords = atom_order_from_wout(wout_path)
    z_order = sort_order(cart_coords, lambda x: x[2]) # sort by z coord

    atom_Hr_order = []
    for z_val in z_order:
        atom_Hr_order.append(z_order_syms[z_val])

    return atom_Hr_order

def orbital_index(atom_Hr_order, sym, orbital, spin, soc=True):
    '''sym is in ["X1", "M", "X2", "X1p", "Mp", "X2p"];
    orbital is in ["pz", "px", "py", "dz2", "dxz", "dyz", "dx2-y2", "dxy"];
    spin is in ["up", "down"].
    spin is ignored if soc is False.
    '''
    if soc:
        num_spins = 2
    else:
        num_spins = 1

    X_syms = ["X1", "X2", "X1p", "X2p"]
    M_syms = ["M", "Mp"]
    X_num_orbitals = num_spins*3
    M_num_orbitals = num_spins*5

    orb_index = 0
    for at_sym in atom_Hr_order:
        if at_sym == sym:
            break

        if at_sym in X_syms:
            orb_index += X_num_orbitals
        elif at_sym in M_syms:
            orb_index += M_num_orbitals
        else:
            raise ValueError("unexpected value in orbital_orders")

    X_orbitals = ["pz", "px", "py"]
    M_orbitals = ["dz2", "dxz", "dyz", "dx2-y2", "dxy"]

    if sym in X_syms:
        this_orbitals = X_orbitals
    elif sym in M_syms:
        this_orbitals = M_orbitals
    else:
        raise ValueError("unrecognized atom position symbol")

    if orbital not in this_orbitals:
        raise ValueError("orbital {} not present for atom {}".format(orbital, sym))

    for orb in this_orbitals:
        if orb == orbital:
            break

        orb_index += num_spins

    if not soc or spin == "up":
        return orb_index
    elif spin == "down":
        return orb_index + 1
    else:
        raise ValueError("unrecognized spin")

def extract_Hk_vals(work, dps, soc):
    orbital_pairs = [("X2_X1p_z_z_uu_K", (1/3, 1/3, 0), ["X2", "pz", "up", "X1p", "pz", "up"]),
            ("X2_X1p_z_z_ud_K", (1/3, 1/3, 0), ["X2", "pz", "up", "X1p", "pz", "down"]),
            ("M_X1p_z2_z_uu_K", (1/3, 1/3, 0), ["M", "dz2", "up", "X1p", "pz", "up"]),
            ("M_X1p_z2_z_ud_K", (1/3, 1/3, 0), ["M", "dz2", "up", "X1p", "pz", "down"]),
            ("X2_Mp_z_z2_uu_K", (1/3, 1/3, 0), ["X2", "pz", "up", "Mp", "dz2", "up"]),
            ("X2_Mp_z_z2_ud_K", (1/3, 1/3, 0), ["X2", "pz", "up", "Mp", "dz2", "down"]),
            ("X2_X1p_z_z_uu_Kp", (-1/3, -1/3, 0), ["X2", "pz", "up", "X1p", "pz", "up"]),
            ("X2_X1p_z_z_ud_Kp", (-1/3, -1/3, 0), ["X2", "pz", "up", "X1p", "pz", "down"]),
            ("M_X1p_z2_z_uu_Kp", (-1/3, -1/3, 0), ["M", "dz2", "up", "X1p", "pz", "up"]),
            ("M_X1p_z2_z_ud_Kp", (-1/3, -1/3, 0), ["M", "dz2", "up", "X1p", "pz", "down"]),
            ("X2_Mp_z_z2_uu_Kp", (-1/3, -1/3, 0), ["X2", "pz", "up", "Mp", "dz2", "up"]),
            ("X2_Mp_z_z2_ud_Kp", (-1/3, -1/3, 0), ["X2", "pz", "up", "Mp", "dz2", "down"]),
            ("X2_X1p_z_z_uu_G", (0, 0, 0), ["X2", "pz", "up", "X1p", "pz", "up"]),
            ("X2_X1p_z_z_ud_G", (0, 0, 0), ["X2", "pz", "up", "X1p", "pz", "down"]),
            ("M_X1p_z2_z_uu_G", (0, 0, 0), ["M", "dz2", "up", "X1p", "pz", "up"]),
            ("M_X1p_z2_z_ud_G", (0, 0, 0), ["M", "dz2", "up", "X1p", "pz", "down"]),
            ("X2_Mp_z_z2_uu_G", (0, 0, 0), ["X2", "pz", "up", "Mp", "dz2", "up"]),
            ("X2_Mp_z_z2_ud_G", (0, 0, 0), ["X2", "pz", "up", "Mp", "dz2", "down"]),
            ("X2_X1p_z_z_uu_M", (1/2, 0, 0), ["X2", "pz", "up", "X1p", "pz", "up"]),
            ("X2_X1p_z_z_ud_M", (1/2, 0, 0), ["X2", "pz", "up", "X1p", "pz", "down"]),
            ("M_X1p_z2_z_uu_M", (1/2, 0, 0), ["M", "dz2", "up", "X1p", "pz", "up"]),
            ("M_X1p_z2_z_ud_M", (1/2, 0, 0), ["M", "dz2", "up", "X1p", "pz", "down"]),
            ("X2_Mp_z_z2_uu_M", (1/2, 0, 0), ["X2", "pz", "up", "Mp", "dz2", "up"]),
            ("X2_Mp_z_z2_ud_M", (1/2, 0, 0), ["X2", "pz", "up", "Mp", "dz2", "down"]),
            ("X2_X1p_z_z_uu_Mp", (-1/2, 0, 0), ["X2", "pz", "up", "X1p", "pz", "up"]),
            ("X2_X1p_z_z_ud_Mp", (-1/2, 0, 0), ["X2", "pz", "up", "X1p", "pz", "down"]),
            ("M_X1p_z2_z_uu_Mp", (-1/2, 0, 0), ["M", "dz2", "up", "X1p", "pz", "up"]),
            ("M_X1p_z2_z_ud_Mp", (-1/2, 0, 0), ["M", "dz2", "up", "X1p", "pz", "down"]),
            ("X2_Mp_z_z2_uu_Mp", (-1/2, 0, 0), ["X2", "pz", "up", "Mp", "dz2", "up"]),
            ("X2_Mp_z_z2_ud_Mp", (-1/2, 0, 0), ["X2", "pz", "up", "Mp", "dz2", "down"])]

    Hk_vals = {}
    for d, prefix in dps:
        Hr = get_Hr(work, prefix)
        atom_Hr_order = get_atom_order(work, prefix)

        for label, klat, orb_types in orbital_pairs:
            i_sym, i_orbital, i_spin = orb_types[0], orb_types[1], orb_types[2]
            j_sym, j_orbital, j_spin = orb_types[3], orb_types[4], orb_types[5]

            i_index = orbital_index(atom_Hr_order, i_sym, i_orbital, i_spin, soc)
            j_index = orbital_index(atom_Hr_order, j_sym, j_orbital, j_spin, soc)

            Hk = Hk_recip(klat, Hr)
            val = Hk[i_index, j_index]

            re_label, im_label = "{}_re".format(label), "{}_im".format(label)
            if re_label not in Hk_vals:
                Hk_vals[re_label] = []
            if im_label not in Hk_vals:
                Hk_vals[im_label] = []

            Hk_vals[re_label].append(val.real)
            Hk_vals[im_label].append(val.imag)

    return Hk_vals

def plot_d_vals(plot_name, title, dps, values):
    xs, ys = [], []
    xs_set, ys_set = set(), set()
    for d, prefix in dps:
        xs.append(d[0])
        ys.append(d[1])
        xs_set.add(d[0])
        ys_set.add(d[1])

    num_xs = len(xs_set)
    num_ys = len(ys_set)

    C_E = np.array(values).reshape((num_xs, num_ys))

    plt.xlabel("$d_a$")
    plt.ylabel("$d_b$")

    num_ticks_xs, num_ticks_ys = 5, 5
    d_ticks_xs = []
    for x in np.linspace(0.0, 1.0, num_ticks_xs, endpoint=True):
        d_ticks_xs.append("{:.2f}".format(x))
    d_ticks_ys = []
    for y in np.linspace(0.0, 1.0, num_ticks_ys, endpoint=True):
        d_ticks_ys.append("{:.2f}".format(y))

    plt.xticks(np.linspace(0.0, num_xs-1, num_ticks_xs, endpoint=True), d_ticks_xs)
    plt.yticks(np.linspace(0.0, num_ys-1, num_ticks_ys, endpoint=True), d_ticks_ys)

    plt.imshow(C_E.T, origin='lower', interpolation='none', cmap=cm.viridis)
    plt.colorbar()
    plt.title(title)
    plt.savefig("{}.png".format(plot_name), bbox_inches='tight', dpi=500)

    plt.clf()

def _main():
    gconf = global_config()
    work = os.path.expandvars(gconf["work_base"])
    
    global_prefix = "MoSe2_WSe2"
    prefixes = get_prefixes(work, global_prefix)
    ds = ds_from_prefixes(prefixes)

    ds, prefixes = wrap_cell(ds, prefixes)
    dps = sorted_d_group(ds, prefixes)

    energies = get_energies(work, dps)
    energies_rel_meV = energies_relative_to(energies, dps, (0.0, 0.0))

    E_title = "$\\Delta E$ [meV]"
    E_plot_name = "{}_energies".format(global_prefix)
    plot_d_vals(E_plot_name, E_title, dps, energies_rel_meV)

    soc = True
    Hk_vals = extract_Hk_vals(work, dps, soc)

    for label, this_vals in Hk_vals.items():
        title = label
        plot_name = "{}_{}".format(global_prefix, label)
        plot_d_vals(plot_name, title, dps, this_vals)

if __name__ == "__main__":
    _main()

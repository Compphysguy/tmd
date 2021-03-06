import os
import argparse
import json
from multiprocessing import Pool
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.cm as cm
from tmd.pwscf.parseScf import total_energy_eV_from_scf, fermi_from_scf, D_from_scf
from tmd.wannier.parseWout import atom_order_from_wout
from tmd.wannier.bands import Hk_recip
from tmd.wannier.findGaps import HrFindGaps
from tmd.bilayer.bilayer_util import global_config
from tmd.bilayer.dgrid import get_prefixes
from tmd.bilayer.wannier import get_Hr

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

    # has the structure {"val_label_1": [val1(d1), val1(d2), ...],
    #       "val_label_2": [val2(d1), val2(d2), ...], ...}
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

def system_all_gaps(work, prefix, E_below_fermi, E_above_fermi, num_dos, na, nb):
    HrPath = os.path.join(work, prefix, "wannier", "{}_hr.dat".format(prefix))
    scf_path = os.path.join(work, prefix, "wannier", "scf.out")

    E_F = fermi_from_scf(scf_path)
    minE = E_F - E_below_fermi
    maxE = E_F + E_above_fermi

    D = D_from_scf(scf_path)
    R = 2 * np.pi * np.linalg.inv(D)
    nc = 1

    gaps, dos_vals, E_vals = HrFindGaps(minE, maxE, num_dos, na, nb, nc, R, HrPath)
    return gaps, dos_vals, E_vals

def find_gaps(work, dps, E_below_fermi, E_above_fermi, num_dos, na, nb):
    gap_call_args = []
    for d, prefix in dps:
        gap_call_args.append((work, prefix, E_below_fermi, E_above_fermi, num_dos, na, nb))

    with Pool() as pool:
        all_gaps_output = pool.starmap(system_all_gaps, gap_call_args)

    gaps = []
    for d_index, (d, prefix) in enumerate(dps):
        this_gaps = all_gaps_output[d_index][0]
        scf_path = os.path.join(work, prefix, "wannier", "scf.out")
        E_F = fermi_from_scf(scf_path)

        gap_at_fermi = None
        for gap_interval in this_gaps:
            # Check that either E_F is inside the gap, or E_F is close to
            # valence band maximum or conduction band minimum.
            gap_in_band_tolerance = 1e-2 # 10 meV tolerance
            if ((E_F >= gap_interval[0] and E_F <= gap_interval[1])
                    or abs(gap_interval[0] - E_F) < gap_in_band_tolerance
                    or abs(E_F - gap_interval[1]) < gap_in_band_tolerance):
                gap_at_fermi = gap_interval
                break

        if gap_at_fermi is not None:
            gap_val = gap_at_fermi[1] - gap_at_fermi[0]
            gaps.append(gap_val)
        else:
            gaps.append(0.0)

    return gaps

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
    parser = argparse.ArgumentParser(description="Plot various quantities as function of displacement")
    parser.add_argument("--subdir", type=str, default=None,
            help="Subdirectory under work_base where calculation was run")
    parser.add_argument('--global_prefix', type=str, default="MoS2_WS2",
            help="Calculation global prefix")
    args = parser.parse_args()

    gconf = global_config()
    work = os.path.expandvars(gconf["work_base"])
    if args.subdir is not None:
        work = os.path.join(work, args.subdir)
    
    prefixes = get_prefixes(work, args.global_prefix)
    ds = ds_from_prefixes(prefixes)

    ds, prefixes = wrap_cell(ds, prefixes)
    dps = sorted_d_group(ds, prefixes)

    write_out_data = {"_ds": []}
    for d, prefix in dps:
        write_out_data["_ds"].append(d)

    energies = get_energies(work, dps)
    energies_rel_meV = energies_relative_to(energies, dps, (0.0, 0.0))

    E_title = "$\\Delta E$ [meV]"
    E_plot_name = "{}_energies".format(args.global_prefix)
    plot_d_vals(E_plot_name, E_title, dps, energies_rel_meV)
    write_out_data["meV_relative_total_energy"] = energies_rel_meV

    soc = True
    Hk_vals = extract_Hk_vals(work, dps, soc)

    for label, this_vals in Hk_vals.items():
        title = label
        plot_name = "{}_{}".format(args.global_prefix, label)
        plot_d_vals(plot_name, title, dps, this_vals)
        write_out_data["eV_{}".format(label)] = this_vals

    na, nb = 16, 16
    num_dos = 1000
    E_below_fermi, E_above_fermi = 3.0, 3.0
    gaps = find_gaps(work, dps, E_below_fermi, E_above_fermi, num_dos, na, nb)

    gap_plot_title = "Gaps [eV]"
    gap_plot_name = "{}_gaps".format(args.global_prefix)
    plot_d_vals(gap_plot_name, gap_plot_title, dps, gaps)
    write_out_data["eV_overall_gap"] = gaps

    with open("{}_plot_ds_data.json".format(args.global_prefix), 'w') as fp:
        json.dump(write_out_data, fp)

if __name__ == "__main__":
    _main()

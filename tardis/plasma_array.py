#Calculations of the Plasma conditions
import numpy as np
import logging
from astropy import constants
import pandas as pd
import os
from scipy import interpolate

from tardis import macro_atom, config_reader


logger = logging.getLogger(__name__)

k_B_cgs = constants.k_B.cgs.value
c_cgs = constants.c.cgs.value
h_cgs = constants.h.cgs.value
m_e_cgs = constants.m_e.cgs.value
e_charge_gauss = constants.e.gauss.value

#Defining sobolev constant
sobolev_coefficient = ((np.pi * e_charge_gauss ** 2) / ( m_e_cgs * c_cgs))


class PlasmaException(Exception):
    pass

class PopulationInversionException(PlasmaException):
    pass

def intensity_black_body(nu, T):
    """
        Calculate the intensity of a black-body according to the following formula

        .. math::
            I(\\nu, T) = \\frac{2h\\nu^3}{c^2}\frac{1}{e^{h\\nu \\beta_\\textrm{rad}} - 1}

    """
    beta_rad = 1 / (k_B_cgs * T)

    return (2 * (h_cgs * nu ** 3) / (c_cgs ** 2)) / (
        np.exp(h_cgs * nu * beta_rad) - 1)


class BasePlasmaArray(object):
    """
    Model for BasePlasma

    Parameters
    ----------

    t_rad : `~float`
        radiation temperature in K

    w : `~float`
        dilution factor W

    number_density : `~pandas.Series`
        Series where the index describes the atomic number and the value is the number density

    atom_data : :class:`~tardis.atomic.AtomData` object
        with the necessary information
    time_explosion : `~float`
        time since explosion in seconds

    j_blues=None : :class:`~numpy.ndarray`, optional
        mean intensity at the blue side of the line (the default is `None` and implies that they are calculated
        according to the selected Plasma)

    t_electron : `~float`, optional
        electron temperature in K (the default is `None` and implies to set it to 0.9 * t_rad)

    nlte_species : `~list`-like, optional
        what species to use for NLTE calculations (e.g. [(20,1), (14, 1)] for Ca II and Si II; default is [])

    nlte_options={} : `dict`-like, optional
        NLTE options mainly for debugging purposes - please refer to the configuration documentation for additional
        information

    zone_id=None : `int`, optional
        What zone_id this plasma represents. Mainly for logging purposes.

    saha_treatment : `str`, optional
        Describes what Saha treatment to use for ionization calculations. The options are `lte` or `nebular`

    Returns
    -------

    `tardis.plasma.BasePlasma`
    """

    @classmethod
    def from_abundance(cls, abundances, density, atom_data, time_explosion, nlte_config=None, saha_treatment='lte'):
        """
        Initializing the abundances from the a dictionary like {'Si':0.5, 'Fe':0.5} and a density.
        All other parameters are the same as the normal initializer


        Parameters
        ----------

        abundances : `~dict`
            A dictionary with the abundances for each element, e.g. {'Fe':0.5, 'Ni':0.5}



        density : `~float`
            density in g/cm^3


        Returns
        -------

        `Baseplasma` object
        """

        atomic_numbers = np.array([config_reader.element_symbol2atomic_number(symbol, atom_data)
                                   for symbol in abundances])

        number_density = pd.Series(index=atomic_numbers)

        for symbol in abundance:
            element_symbol = reformat_element_symbol(symbol)
            if element_symbol not in atom_data.symbol2atomic_number:
                raise ValueError('Element %s provided in config unknown' % element_symbol)

            z = atom_data.symbol2atomic_number[element_symbol]

            number_density.ix[z] = abundance[symbol]

        number_density = number_density[~number_density.isnull()]

        abundance_sum = number_density.sum()

        if abs(abundance_sum - 1.) > 0.02:
            logger.warning('Abundances do not sum up to 1 (%g)- normalizing', abundance_sum)

        number_density /= abundance_sum

        number_density *= density
        number_density /= atom_data.atom_data.mass[number_density.index]

        return cls(t_rad=t_rad, w=w, number_density=number_density, atom_data=atom_data, j_blues=j_blues,
                   time_explosion=time_explosion, t_electron=t_electron, zone_id=zone_id,
                   nlte_species=nlte_species, nlte_options=nlte_options, saha_treatment=saha_treatment)

    @classmethod
    def from_hdf5(cls, hdf5store):
        raise NotImplementedError()


    def __init__(self, number_densities, atom_data, time_explosion, nlte_config=None, saha_treatment='lte'):
        self.number_densities = number_densities
        self.atom_data = atom_data
        self.time_explosion = time_explosion
        self.nlte_config = nlte_config

        self.electron_densities = self.number_densities.sum(axis=0)

        self.level_populations = pd.DataFrame(index=self.atom_data.levels.index, columns=number_densities.columns,
                                              dtype=np.float64)

        if saha_treatment == 'lte':
            self.calculate_saha = self.calculate_saha_lte
        elif saha_treatment == 'nebular':
            self.calculate_saha = self.calculate_saha_nebular
        else:
            raise ValueError('keyword "saha_treatment" can only be "lte" or "nebular" - %s chosen' % saha_treatment)


    #Properties

    @property
    def t_rads(self):
        return self._t_rads

    @t_rads.setter
    def t_rads(self, value):
        self._t_rads = value
        self.beta_rads = (1 / (k_B_cgs * self._t_rads))
        self.g_electrons = ((2 * np.pi * m_e_cgs / self.beta_rads) / (h_cgs ** 2)) ** 1.5

    @property
    def t_electrons(self):
        if self._t_electrons is None:
            return self.t_rads * self.link_t_rad_to_t_electron
        else:
            return self._t_electrons

    @t_electrons.setter
    def t_electrons(self, value):
        if value is None:
            self.link_t_rad_to_t_electron = 0.9
            self._t_electrons = None
        else:
            self._t_electrons = value

        self.beta_electrons = 1 / (k_B_cgs * self.t_electrons)


    #Functions

    def update_radiationfield(self, t_rads, ws, j_blues=None, t_electrons=None, n_e_convergence_threshold=0.05):
        """
            This functions updates the radiation temperature `t_rad` and calculates the beta_rad
            Parameters. Then calculating :math:`g_e=\\left(\\frac{2 \\pi m_e k_\\textrm{B}T}{h^2}\\right)^{3/2}`.
            Next will calculate the partition functions, followed by the phis
            (using `calculate_saha`).

            Parameters
            ----------
            t_rad : float

            n_e_convergence_threshold : float
                The electron density convergence threshold. The number to stop when iterating over calculating the
                ionization balance.

       """

        self.t_rads = t_rads
        if t_electrons is None:
            self.t_electrons = None
        self.ws = ws
        self.j_blues=j_blues
        self.level_population_proportionalities, self.partition_functions = self.calculate_partition_functions()




        #Calculate the Saha ionization balance fractions
        phis = self.calculate_saha()
        #initialize electron density with the sum of number densities
        n_e_iterations = 0

        while True:
            self.calculate_ion_populations(phis)
            ion_numbers = self.ion_populations.index.get_level_values(1).values
            ion_numbers = ion_numbers.reshape((ion_numbers.shape[0], 1))
            new_electron_densities = (self.ion_populations.values * ion_numbers).sum(axis=0)

            if np.any(np.isnan(new_electron_densities)):
                raise PlasmaException('electron density just turned "nan" - aborting')

            n_e_iterations += 1
            if n_e_iterations > 100:
                logger.warn('electron density iterations above 100 (%d) - something is probably wrong', n_e_iterations)

            if np.all(np.abs(new_electron_densities - self.electron_densities) / self.electron_densities <
                    n_e_convergence_threshold): break

            self.electron_densities = 0.5 * (new_electron_densities + self.electron_densities)

        self.calculate_level_populations()
        self.tau_sobolevs = self.calculate_tau_sobolev()

        if self.nlte_config is not None and self.nlte_config.species:
            raise NotImplementedError()
            self.calculate_nlte_level_populations()



    def calculate_partition_functions(self, initialize_nlte=False):
        """
        Calculate partition functions for the ions using the following formula, where
        :math:`i` is the atomic_number, :math:`j` is the ion_number and :math:`k` is the level number.

        .. math::
            Z_{i,j} = \\sum_{k=0}^{max(k)_{i,j}} g_k \\times e^{-E_k / (k_\\textrm{b} T)}



        if self.initialize is True set the first time the partition functions are initialized.
        This will set a self.partition_functions and initialize with LTE conditions.


        Returns
        -------

        partition_functions : `~astropy.table.Table`
            with fields atomic_number, ion_number, partition_function

        """
        levels = self.atom_data.levels
        level_population_proportional_array = levels.g.values[np.newaxis].T *\
                                              np.exp(np.outer(levels.energy.values, -self.beta_rads))
        level_population_proportionalities = pd.DataFrame(level_population_proportional_array,
                                                               index=self.atom_data.levels.index,
                                                               columns=np.arange(len(self.t_rads)), dtype=np.float64)

        #level_props = self.level_population_proportionalities

        partition_functions = level_population_proportionalities[self.atom_data.levels.metastable].groupby(
            level=['atomic_number', 'ion_number']).sum()
        partition_functions_non_meta = self.ws * level_population_proportionalities[~self.atom_data.levels.metastable].groupby(
            level=['atomic_number', 'ion_number']).sum()

        partition_functions.ix[partition_functions_non_meta.index] += partition_functions_non_meta

        if self.nlte_config is not None and self.nlte_config.species != [] and not initialize_nlte:
            for species in self.nlte_config.species:
                self.partition_functions.ix[species] = self.atom_data.levels.g.ix[species].ix[0] * \
                                                       (self.level_populations.ix[species] /
                                                        self.level_populations.ix[species].ix[0]).sum()

        return level_population_proportionalities, partition_functions

    def calculate_saha_lte(self):
        """
        Calculating the ionization equilibrium using the Saha equation, where i is atomic number,
        j is the ion_number, :math:`n_e` is the electron density, :math:`Z_{i, j}` are the partition functions
        and :math:`\chi` is the ionization energy.

        .. math::


            \\Phi_{i,j} = \\frac{N_{i, j+1} n_e}{N_{i, j}}

            \\Phi_{i, j} = g_e \\times \\frac{Z_{i, j+1}}{Z_{i, j}} e^{-\chi_{j\\rightarrow j+1}/k_\\textrm{B}T}

        """

        logger.debug('Calculating Saha using LTE approximation')

        def calculate_phis(group):
            return group[1:] / group[:-1].values

        phis = self.partition_functions.groupby(level='atomic_number').apply(calculate_phis)

        phis = pd.DataFrame(phis.values, index=phis.index.droplevel(0))

        phi_coefficient = self.g_electrons * \
                          np.exp(np.outer(self.atom_data.ionization_data.ionization_energy.ix[phis.index].values,
                                          -self.beta_rads))

        return phis * phi_coefficient

    def calculate_saha_nebular(self):
        """
        Calculating the ionization equilibrium using the Saha equation, where i is atomic number,
        j is the ion_number, :math:`n_e` is the electron density, :math:`Z_{i, j}` are the partition functions
        and :math:`\chi` is the ionization energy. For the `NebularPlasma` we first calculate the
        ionization balance assuming LTE conditions (:math:`\\Phi_{i, j}(\\textrm{LTE})`) and use factors to more accurately
        describe the plasma. The two important factors are :math:`\\zeta` - a correction factor to take into account
        ionizations from excited states. The second factor is :math:`\\delta` , adjusting the ionization balance for the fact that
        there's more line blanketing in the blue.

        The :math:`\\zeta` factor for different temperatures is read in to the `~tardis.atomic.NebularAtomData` and then
        interpolated for the current temperature.

        The :math:`\\delta` factor is calculated with :meth:`calculate_radiation_field_correction`.

        Finally the ionization balance is adjusted (as equation 14 in :cite:`1993A&A...279..447M`):

        .. math::


            \\Phi_{i,j} =& \\frac{N_{i, j+1} n_e}{N_{i, j}} \\\\

            \\Phi_{i, j} =& W \\times[\\delta \\zeta + W ( 1 - \\zeta)] \\left(\\frac{T_\\textrm{e}}{T_\\textrm{R}}\\right)^{1/2}
            \\Phi_{i, j}(\\textrm{LTE})

        """

        logger.debug('Calculating Saha using Nebular approximation')
        phis = self.calculate_saha_lte()

        delta = self.calculate_radfield_correction()

        zeta_data = self.atom_data.zeta_data
        zeta = interpolate.interp1d(zeta_data.columns.values, zeta_data.ix[phis.index].values)(self.t_rads)

        phis *= self.ws * (delta.ix[phis.index] * zeta + self.ws * (1 - zeta)) * \
                (self.t_electrons / self.t_rads) ** .5

        return phis

    def calculate_radfield_correction(self, departure_coefficient=None, chi_threshold_species=(20, 1)):
        """
        Calculating radiation field correction factors according to Mazzali & Lucy 1993 (:cite:`1993A&A...279..447M`; henceforth ML93)


        In ML93 the radiation field correction factor is denoted as :math:`\\delta` and is calculated in Formula 15 & 20

        The radiation correction factor changes according to a ionization energy threshold :math:`\\chi_\\textrm{T}`
        and the species ionization threshold (from the ground state) :math:`\\chi_0`.

        For :math:`\\chi_\\textrm{T} \\ge \\chi_0`

        .. math::
            \\delta = \\frac{T_\\textrm{e}}{b_1 W T_\\textrm{R}} \\exp(\\frac{\\chi_\\textrm{T}}{k T_\\textrm{R}} -
            \\frac{\\chi_0}{k T_\\textrm{e}})

        For :math:`\\chi_\\textrm{T} < \\chi_0`

        .. math::
            \\delta = 1 - \\exp(\\frac{\\chi_\\textrm{T}}{k T_\\textrm{R}} - \\frac{\\chi_0}{k T_\\textrm{R}}) + \\frac{T_\\textrm{e}}{b_1 W T_\\textrm{R}} \\exp(\\frac{\\chi_\\textrm{T}}{k T_\\textrm{R}} -
            \\frac{\\chi_0}{k T_\\textrm{e}}),

        where :math:`T_\\textrm{R}` is the radiation field Temperature, :math:`T_\\textrm{e}` is the electron temperature and W is the
        dilution factor.

        Parameters
        ----------
        phi_table : `~astropy.table.Table`
            a table containing the field 'atomic_number', 'ion_number', 'phi'

        departure_coefficient : `~float` or `~None`, optional
            departure coefficient (:math:`b_1` in ML93) For the default (`None`) it is set to 1/W.

        chi_threshold_species : `~tuple`, optional
            This describes which ionization energy to use for the threshold. Default is Calcium II
            (1044 Angstrom; useful for Type Ia)
            For Type II supernovae use Lyman break (912 Angstrom) or (1,1) as the tuple

        Returns
        -------

        This function adds a field 'delta' to the phi table given to the function

        """
        #factor delta ML 1993
        if departure_coefficient is None:
            departure_coefficient = 1. / self.ws

        ionization_data = self.atom_data.ionization_data

        chi_threshold = ionization_data.ionization_energy.ix[chi_threshold_species]

        radiation_field_correction = (self.t_electrons / (departure_coefficient * self.ws * self.t_rads)) *\
                                     np.exp(self.beta_rads * chi_threshold - np.outer(
                                            ionization_data.ionization_energy.values, self.beta_electrons))


        less_than_chi_threshold = (ionization_data.ionization_energy < chi_threshold).values

        radiation_field_correction[less_than_chi_threshold] += 1 - \
                                                               np.exp(self.beta_rads * chi_threshold - np.outer(
                                                                      ionization_data.ionization_energy.values
                                                                      [less_than_chi_threshold], self.beta_rads))
        return pd.DataFrame(radiation_field_correction, columns=np.arange(len(self.t_rads)),
                            index=ionization_data.index)



    def calculate_ion_populations(self, phis, ion_zero_threshold=1e-20):
        """
        Calculate the ionization balance

        .. math::
            N(X) = N_1 + N_2 + N_3 + \\dots

            N(X) = (N_2/N_1) \\times N_1 + (N3/N2) \\times (N_2/N_1) \\times N_1 + \\dots

            N(X) = N_1(1 + N_2/N_1 + (N_3/N_2) \\times (N_2/N_1) + \\dots

            N(X) = N_1(1+ \\Phi_{i,j}/N_e + \\Phi_{i, j}/N_e \\times \\Phi_{i, j+1}/N_e + \\dots)


        """
        #TODO see if self.ion_populations is None is needed (first class should be enough)
        if not hasattr(self, 'ion_populations'):
            self.ion_populations = pd.DataFrame(index=self.partition_functions.index.copy(),
                                                columns=np.arange(len(self.t_rads)), dtype=np.float64)

        for atomic_number, groups in phis.groupby(level='atomic_number'):
            current_phis = (groups / self.electron_densities).replace(np.nan, 0.0).values
            phis_product = np.cumproduct(current_phis, axis=0)

            neutral_atom_density = self.number_densities.ix[atomic_number] / (1 + np.sum(phis_product, axis=0))



            self.ion_populations.ix[atomic_number].values[0] = neutral_atom_density.values
            self.ion_populations.ix[atomic_number].values[1:] = neutral_atom_density.values * phis_product
            self.ion_populations[self.ion_populations < ion_zero_threshold] = 0.0

    def calculate_level_populations(self, initialize_nlte=False):
        """
        Calculate the level populations and putting them in the column 'number-density' of the self.levels table.
        :math:`N` denotes the ion number density calculated with `calculate_ionization_balance`, i is the atomic number,
        j is the ion number and k is the level number. For non-metastable levels we add the dilution factor (W) to the calculation.

        .. math::

            N_{i, j, k}(\\textrm{metastable}) &= \\frac{g_k}{Z_{i, j}}\\times N_{i, j} \\times e^{-\\beta_\\textrm{rad} E_k} \\\\
            N_{i, j, k}(\\textrm{not metastable}) &= W\\frac{g_k}{Z_{i, j}}\\times N_{i, j} \\times e^{-\\beta_\\textrm{rad} E_k} \\\\


        This function updates the 'number_density' column on the levels table (or adds it if non-existing)
        """
        Z = self.partition_functions.ix[self.atom_data.levels.index.droplevel(2)].values

        ion_number_density = self.ion_populations.ix[self.atom_data.levels.index.droplevel(2)].values


        level_populations = (ion_number_density / Z) * self.level_population_proportionalities

        #only change between lte plasma and nebular
        level_populations[~self.atom_data.levels.metastable] *= np.min([self.ws, np.ones_like(self.ws)],axis=0)


        if initialize_nlte:
            self.level_populations.update(level_populations)
        else:
            self.level_populations.update(level_populations[~self.atom_data.nlte_data.nlte_levels_mask])




    def calculate_tau_sobolev(self):
        """
        This function calculates the Sobolev optical depth :math:`\\tau_\\textrm{Sobolev}`



        .. math::
            C_\\textrm{Sobolev} = \\frac{\\pi e^2}{m_e c}

            \\tau_\\textrm{Sobolev} = C_\\textrm{Sobolev}\,  \\lambda\\, f_{\\textrm{lower}\\rightarrow\\textrm{upper}}\\,
                t_\\textrm{explosion}\, N_\\textrm{lower}



        .. note::
            Currently we're ignoring the term for stimulated emission:
                :math:`(1 - \\frac{g_\\textrm{lower}}{g_\\textrm{upper}}\\frac{N_\\textrm{upper}}{N_\\textrm{lower}})`


        """

        f_lu = self.atom_data.lines['f_lu'].values
        f_lu = f_lu.reshape((f_lu.shape[0], 1))
        wavelength = self.atom_data.lines['wavelength_cm'].values
        wavelength = wavelength.reshape((wavelength.shape[0], 1))

        n_lower = self.level_populations.values[self.atom_data.lines_lower2level_idx]
        n_upper = self.level_populations.values[self.atom_data.lines_upper2level_idx]


        g_lower = self.atom_data.levels.g.values[self.atom_data.lines_lower2level_idx]
        g_lower = g_lower.reshape((g_lower.shape[0], 1))
        g_upper = self.atom_data.levels.g.values[self.atom_data.lines_upper2level_idx]
        g_upper = g_upper.reshape((g_upper.shape[0], 1))

        self.stimulated_emission_factor = 1 - ((g_lower * n_upper) / (g_upper * n_lower))

        # getting rid of the obvious culprits
        self.stimulated_emission_factor[(n_lower == 0.0) & (n_upper == 0.0)] = 0.0
        self.stimulated_emission_factor[np.isneginf(self.stimulated_emission_factor)] = 0.0

        if self.nlte_config is not None and self.nlte_config.species != []:
            nlte_lines_mask = np.zeros(self.stimulated_emission_factor.shape[0]).astype(bool)
            for species in self.nlte_config.species:
                nlte_lines_mask |= (self.atom_data.lines_data.atomic_number == nlte_species[0]) & \
                                   (self.atom_data.lines_data.ion_number == nlte_species[1])
            self.stimulated_emission_factor[(self.stimulated_emission_factor < 0) & nlte_lines_mask] = 0.0


        tau_sobolevs = sobolev_coefficient * f_lu * wavelength * self.time_explosion * n_lower * \
                    self.stimulated_emission_factor

        return pd.DataFrame(tau_sobolevs, index=self.atom_data.lines.index, columns=np.arange(len(self.t_rads)))



    def calculate_nlte_level_populations(self):
        """
        Calculating the NLTE level populations for specific ions

        """

        if not hasattr(self, 'beta_sobolevs'):
            self.beta_sobolevs = np.zeros_like(self.atom_data.lines['nu'].values)

        macro_atom.calculate_beta_sobolev(self.tau_sobolevs, self.beta_sobolevs)

        if self.nlte_config.get('coronal_approximation', False):
            beta_sobolevs = np.ones_like(self.beta_sobolevs)
            j_blues = np.zeros_like(self.j_blues)
        else:
            beta_sobolevs = self.beta_sobolevs
            j_blues = self.j_blues

        if self.nlte_config.get('classical_nebular', False):
            print "setting classical nebular = True"
            beta_sobolevs[:] = 1.0

        for species in self.nlte_config.species:
            logger.info('Calculating rates for species %s', species)
            number_of_levels = self.level_populations.ix[species].size

            level_populations = self.level_populations.ix[species].values
            lnl = self.atom_data.nlte_data.lines_level_number_lower[species]
            lnu = self.atom_data.nlte_data.lines_level_number_upper[species]

            lines_index = self.atom_data.nlte_data.lines_idx[species]
            A_uls = self.atom_data.nlte_data.A_uls[species]
            B_uls = self.atom_data.nlte_data.B_uls[species]
            B_lus = self.atom_data.nlte_data.B_lus[species]

            r_lu_index = lnu * number_of_levels + lnl
            r_ul_index = lnl * number_of_levels + lnu

            r_ul_matrix = np.zeros((number_of_levels, number_of_levels), dtype=np.float64)
            r_ul_matrix.ravel()[r_ul_index] = A_uls
            r_ul_matrix.ravel()[r_ul_index] *= beta_sobolevs[lines_index]

            stimulated_emission_matrix = np.zeros_like(r_ul_matrix)
            stimulated_emission_matrix.ravel()[r_lu_index] = 1 - ((level_populations[lnu] * B_uls) / (
                level_populations[lnl] * B_lus))

            stimulated_emission_matrix[stimulated_emission_matrix < 0.] = 0.0

            r_lu_matrix = np.zeros_like(r_ul_matrix)
            r_lu_matrix.ravel()[r_lu_index] = B_lus * j_blues[lines_index] * beta_sobolevs[lines_index]
            r_lu_matrix *= stimulated_emission_matrix

            collision_matrix = self.atom_data.nlte_data.get_collision_matrix(species,
                                                                             self.t_electron) * self.electron_density

            rates_matrix = r_lu_matrix + r_ul_matrix + collision_matrix

            for i in xrange(number_of_levels):
                rates_matrix[i, i] = -np.sum(rates_matrix[:, i])

            rates_matrix[0] = 1.0

            x = np.zeros(rates_matrix.shape[0])
            x[0] = 1.0
            relative_level_populations = np.linalg.solve(rates_matrix, x)

            self.level_populations.ix[species] = relative_level_populations * self.ion_populations.ix[species]

            return

    def calculate_transition_probabilities(self):
        """
            Updating the Macro Atom computations
        """
        macro_atom_data = self.atom_data.macro_atom_data
        macro_tau_sobolevs = self.tau_sobolevs.ix[macro_atom_data.transition_line_id]


        beta_sobolevs = np.zeros_like(macro_tau_sobolevs.values)
        beta_sobolevs_f = beta_sobolevs.ravel(order='F')

        macro_atom.calculate_beta_sobolev(macro_tau_sobolevs.values.flatten(), beta_sobolevs_f)


        transition_probabilities = macro_atom_data.transition_probability.values[np.newaxis].T * beta_sobolevs

        transition_up_filter = (macro_atom_data.transition_type == 1).values
        macro_atom_transition_up_filter = macro_atom_data.lines_idx.values[transition_up_filter]
        j_blues = self.j_blues[macro_atom_transition_up_filter]
        macro_stimulated_emission = self.stimulated_emission_factor[macro_atom_transition_up_filter]

        transition_probabilities[transition_up_filter] *= j_blues * macro_stimulated_emission


        #Normalizing the probabilities
        block_references = np.hstack((self.atom_data.macro_atom_references.block_references,
                                      len(macro_atom_data)))
        macro_atom.normalize_transition_probabilities(transition_probabilities, block_references)
        return pd.DataFrame(transition_probabilities, index=macro_atom_data.transition_line_id,
                     columns=self.tau_sobolevs.columns)

    def set_j_blues(self, j_blues=None):
        if j_blues is None:
            self.j_blues = self.w * intensity_black_body(self.atom_data.lines['nu'].values, self.t_rad)
        else:
            self.j_blues = j_blues

    def calculate_bound_free(self):
        #TODO DOCUMENTATION missing!!!
        """
        None

        """
        nu_bins = range(1000, 10000, 1000) #TODO: get the binning from the input file.
        try:
            bf = np.zeros(len(self.atom_data.levels), len(self.atom_data.selected_atomic_numbers), len(nu_bins))
        except AttributeError:
            logger.critical("Err creating the bf array.")

        phis = self.calculate_saha()
        nnlevel = self.level_populations
        for nu in nu_bins:
            for i, (level_id, level) in enumerate(self.atom_data.levels.iterrows()):
                atomic_number = level.name[0]
                ion_number = level.name[1]
                level_number = level.name[2]
                sigma_bf_th = self.atom_data.ion_cx_th.ix[atomic_number, ion_number, level_number]
                phi = phis.ix[atomic_number, ion_number]


    def to_hdf5(self, hdf5_store, path, mode='full'):
        """

        param hdf5_store:
        :param path:
        :return:
        """
        if mode == 'full':
            partition_functions_path = os.path.join(path, 'partition_functions')
            self.partition_functions.to_hdf(hdf5_store, partition_functions_path)

            ion_populations_path = os.path.join(path, 'ion_populations')
            self.ion_populations.to_hdf(hdf5_store, ion_populations_path)

            level_populations_path = os.path.join(path, 'level_populations')
            self.level_populations.to_hdf(hdf5_store, level_populations_path)

            j_blues_path = os.path.join(path, 'j_blues')
            pd.Series(self.j_blues).to_hdf(hdf5_store, j_blues_path)

            number_density_path = os.path.join(path, 'number_density')
            self.number_density.to_hdf(hdf5_store, number_density_path)

            tau_sobolevs_path = os.path.join(path, 'tau_sobolevs')
            pd.Series(self.tau_sobolevs).to_hdf(hdf5_store, tau_sobolevs_path)

            transition_probabilities_path = os.path.join(path, 'transition_probabilities')
            transition_probabilities = self.calculate_transition_probabilities()
            pd.Series(transition_probabilities).to_hdf(hdf5_store, transition_probabilities_path)

        else:
            raise NotImplementedError('Currently only mode="full" is supported.')


class LTEPlasma(BasePlasmaArray):
    __doc__ = BasePlasmaArray.__doc__

    @classmethod
    def from_abundance(cls, t_rad, abundance, density, atom_data, time_explosion, j_blues=None, t_electron=None,
                       nlte_config=None, zone_id=None):
        __doc__ = BasePlasma.from_abundance.__doc__
        return super(LTEPlasma, cls).from_abundance(t_rad, 1., abundance, density, atom_data, time_explosion,
                                                    j_blues=j_blues, t_electron=t_electron, nlte_config=nlte_config,
                                                    zone_id=zone_id)

    def __init__(self, number_density, atom_data, time_explosion, j_blues=None, t_electron=None,
                 nlte_config=None, zone_id=None, saha_treatment='lte'):
        super(LTEPlasma, self).__init__(t_rad, w, number_density, atom_data, time_explosion, j_blues=j_blues,
                                        t_electron=t_electron, nlte_config=nlte_config, zone_id=zone_id,
                                        saha_treatment=saha_treatment)


class NebularPlasma(BasePlasmaArray):
    __doc__ = BasePlasmaArray.__doc__

    @classmethod
    def from_abundance(cls, t_rad, w, abundance, density, atom_data, time_explosion, j_blues=None, t_electron=None,
                       nlte_config=None, zone_id=None):
        return super(NebularPlasma, cls).from_abundance(t_rad, w, abundance, density, atom_data, time_explosion,
                                                        j_blues=j_blues, t_electron=t_electron, nlte_config=nlte_config,
                                                        zone_id=zone_id, saha_treatment='nebular')


    def __init__(self, t_rad, w, number_density, atom_data, time_explosion, j_blues=None, t_electron=None,
                 nlte_config=None, zone_id=None, saha_treatment='nebular'):
        super(NebularPlasma, self).__init__(t_rad, w, number_density, atom_data, time_explosion, j_blues=j_blues,
                                            t_electron=t_electron, nlte_config=nlte_config, zone_id=zone_id,
                                            saha_treatment=saha_treatment)













from numpy import int32, int64

from artiq.coredevice import spi2 as spi
from artiq.coredevice import urukul
from artiq.coredevice.urukul import DEFAULT_PROFILE, _RegIOUpdate
from artiq.language.core import at_mu, delay, delay_mu, kernel, now_mu, portable
from artiq.language.types import TBool, TFloat, TInt32, TInt64, TList, TTuple
from artiq.language.units import ms, us

# Work around ARTIQ-Python import machinery
urukul_sta_pll_lock = urukul.urukul_sta_pll_lock
urukul_sta_smp_err = urukul.urukul_sta_smp_err

__all__ = [
    "AD9910",
    "PHASE_MODE_CONTINUOUS", "PHASE_MODE_ABSOLUTE", "PHASE_MODE_TRACKING",
    "RAM_DEST_FTW", "RAM_DEST_POW", "RAM_DEST_ASF", "RAM_DEST_POWASF",
    "RAM_MODE_DIRECTSWITCH", "RAM_MODE_RAMPUP", "RAM_MODE_BIDIR_RAMP",
    "RAM_MODE_CONT_BIDIR_RAMP", "RAM_MODE_CONT_RAMPUP",
]

_PHASE_MODE_DEFAULT = -1
PHASE_MODE_CONTINUOUS = 0
PHASE_MODE_ABSOLUTE = 1
PHASE_MODE_TRACKING = 2

_AD9910_REG_CFR1 = 0x00
_AD9910_REG_CFR2 = 0x01
_AD9910_REG_CFR3 = 0x02
_AD9910_REG_AUX_DAC = 0x03
_AD9910_REG_IO_UPDATE = 0x04
_AD9910_REG_FTW = 0x07
_AD9910_REG_POW = 0x08
_AD9910_REG_ASF = 0x09
_AD9910_REG_SYNC = 0x0a
_AD9910_REG_RAMP_LIMIT = 0x0b
_AD9910_REG_RAMP_STEP = 0x0c
_AD9910_REG_RAMP_RATE = 0x0d
_AD9910_REG_PROFILE0 = 0x0e
_AD9910_REG_PROFILE1 = 0x0f
_AD9910_REG_PROFILE2 = 0x10
_AD9910_REG_PROFILE3 = 0x11
_AD9910_REG_PROFILE4 = 0x12
_AD9910_REG_PROFILE5 = 0x13
_AD9910_REG_PROFILE6 = 0x14
_AD9910_REG_PROFILE7 = 0x15
_AD9910_REG_RAM = 0x16

# RAM destination
RAM_DEST_FTW = 0
RAM_DEST_POW = 1
RAM_DEST_ASF = 2
RAM_DEST_POWASF = 3

# RAM MODES
RAM_MODE_DIRECTSWITCH = 0
RAM_MODE_RAMPUP = 1
RAM_MODE_BIDIR_RAMP = 2
RAM_MODE_CONT_BIDIR_RAMP = 3
RAM_MODE_CONT_RAMPUP = 4

# Default profile for RAM mode
_DEFAULT_PROFILE_RAM = 0


class SyncDataUser:
    def __init__(self, core, sync_delay_seed, io_update_delay):
        self.core = core
        self.sync_delay_seed = sync_delay_seed
        self.io_update_delay = io_update_delay

    @kernel
    def init(self):
        pass


class SyncDataEeprom:
    def __init__(self, dmgr, core, eeprom_str):
        self.core = core

        eeprom_device, eeprom_offset = eeprom_str.split(":")
        self.eeprom_device = dmgr.get(eeprom_device)
        self.eeprom_offset = int(eeprom_offset)

        self.sync_delay_seed = 0
        self.io_update_delay = 0

    @kernel
    def init(self):
        word = self.eeprom_device.read_i32(self.eeprom_offset) >> 16
        sync_delay_seed = word >> 8
        if sync_delay_seed >= 0:
            io_update_delay = word & 0xff
        else:
            io_update_delay = 0
        if io_update_delay == 0xff:  # unprogrammed EEPROM
            io_update_delay = 0
        # With Numpy, type(int32(-1) >> 1) == int64
        self.sync_delay_seed = int32(sync_delay_seed)
        self.io_update_delay = int32(io_update_delay)


class AD9910:
    """
    AD9910 DDS channel on Urukul.

    This class supports a single DDS channel and exposes the DDS,
    the digital step attenuator, and the RF switch.

    :param chip_select: Chip select configuration. On Urukul this is an
        encoded chip select and not "one-hot": 3 to address multiple chips
        (as configured through CFG_MASK_NU), 4-7 for individual channels.
    :param cpld_device: Name of the Urukul CPLD this device is on.
    :param sw_device: Name of the RF switch device. The RF switch is a
        TTLOut channel available as the ``sw`` attribute of this instance.
    :param pll_n: DDS PLL multiplier. The DDS sample clock is
        ``f_ref / clk_div * pll_n`` where ``f_ref`` is the reference frequency and
        ``clk_div`` is the reference clock divider (both set in the parent
        Urukul CPLD instance).
    :param pll_en: PLL enable bit, set to 0 to bypass PLL (default: 1).
        Note that when bypassing the PLL the red front panel LED may remain on.
    :param pll_cp: DDS PLL charge pump setting.
    :param pll_vco: DDS PLL VCO range selection.
    :param sync_delay_seed: ``SYNC_IN`` delay tuning starting value.
        To stabilize the ``SYNC_IN`` delay tuning, run :meth:`tune_sync_delay` once
        and set this to the delay tap number returned (default: -1 to signal no
        synchronization and no tuning during :meth:`init`).
        Can be a string of the form ``eeprom_device:byte_offset`` to read the
        value from a I2C EEPROM, in which case ``io_update_delay`` must be set
        to the same string value.
    :param io_update_delay: ``IO_UPDATE`` pulse alignment delay.
        To align ``IO_UPDATE`` to ``SYNC_CLK``, run :meth:`tune_io_update_delay` and
        set this to the delay tap number returned.
        Can be a string of the form ``eeprom_device:byte_offset`` to read the
        value from a I2C EEPROM, in which case ``sync_delay_seed`` must be set
        to the same string value.
    """

    def __init__(self, dmgr, chip_select, cpld_device, sw_device=None,
                 pll_n=40, pll_cp=7, pll_vco=5, sync_delay_seed=-1,
                 io_update_delay=0, pll_en=1):
        self.kernel_invariants = {"cpld", "core", "bus", "chip_select",
                                  "pll_en", "pll_n", "pll_vco", "pll_cp",
                                  "ftw_per_hz", "sysclk_per_mu", "sysclk",
                                  "sync_data"}
        self.cpld = dmgr.get(cpld_device)
        self.core = self.cpld.core
        self.bus = self.cpld.bus
        assert 3 <= chip_select <= 7
        self.chip_select = chip_select
        if sw_device:
            self.sw = dmgr.get(sw_device)
            self.kernel_invariants.add("sw")
        clk = self.cpld.refclk / [4, 1, 2, 4][self.cpld.clk_div]
        self.pll_en = pll_en
        self.pll_n = pll_n
        self.pll_vco = pll_vco
        self.pll_cp = pll_cp
        if pll_en:
            sysclk = clk * pll_n
            assert clk <= 60e6
            assert 12 <= pll_n <= 127
            assert 0 <= pll_vco <= 5
            vco_min, vco_max = [(370, 510), (420, 590), (500, 700),
                                (600, 880), (700, 950), (820, 1150)][pll_vco]
            assert vco_min <= sysclk / 1e6 <= vco_max
            assert 0 <= pll_cp <= 7
        else:
            sysclk = clk
        assert sysclk <= 1e9
        self.ftw_per_hz = (1 << 32) / sysclk
        self.sysclk_per_mu = int(round(sysclk * self.core.ref_period))
        self.sysclk = sysclk

        if not self.cpld.io_update:
            self.io_update = _RegIOUpdate(self.cpld, self.chip_select)
        else:
            self.io_update = self.cpld.io_update

        if isinstance(sync_delay_seed, str) or isinstance(io_update_delay, str):
            if sync_delay_seed != io_update_delay:
                raise ValueError("When using EEPROM, sync_delay_seed must be "
                                 "equal to io_update_delay")
            self.sync_data = SyncDataEeprom(dmgr, self.core, sync_delay_seed)
        else:
            self.sync_data = SyncDataUser(self.core, sync_delay_seed,
                                          io_update_delay)

        self.phase_mode = PHASE_MODE_CONTINUOUS

    @kernel
    def set_phase_mode(self, phase_mode: TInt32):
        r"""Set the default phase mode for future calls to :meth:`set` and
        :meth:`set_mu`. Supported phase modes are:

        * :const:`PHASE_MODE_CONTINUOUS`: the phase accumulator is unchanged
          when changing frequency or phase. The DDS phase is the sum of the
          phase accumulator and the phase offset. The only discontinuous
          changes in the DDS output phase come from changes to the phase
          offset. This mode is also known as "relative phase mode".
          :math:`\phi(t) = q(t^\prime) + p + (t - t^\prime) f`

        * :const:`PHASE_MODE_ABSOLUTE`: the phase accumulator is reset when
          changing frequency or phase. Thus, the phase of the DDS at the
          time of the change is equal to the specified phase offset.
          :math:`\phi(t) = p + (t - t^\prime) f`

        * :const:`PHASE_MODE_TRACKING`: when changing frequency or phase,
          the phase accumulator is cleared and the phase offset is offset
          by the value the phase accumulator would have if the DDS had been
          running at the specified frequency since a given fiducial
          time stamp. This is functionally equivalent to
          :const:`PHASE_MODE_ABSOLUTE`. The only difference is the fiducial
          time stamp. This mode is also known as "coherent phase mode".
          The default fiducial time stamp is 0.
          :math:`\phi(t) = p + (t - T) f`

        Where:

        * :math:`\phi(t)`: the DDS output phase
        * :math:`q(t) = \phi(t) - p`: DDS internal phase accumulator
        * :math:`p`: phase offset
        * :math:`f`: frequency
        * :math:`t^\prime`: time stamp of setting :math:`p`, :math:`f`
        * :math:`T`: fiducial time stamp
        * :math:`t`: running time

        .. warning:: This setting may become inconsistent when used as part of
            a DMA recording. When using DMA, it is recommended to specify the
            phase mode explicitly when calling :meth:`set` or :meth:`set_mu`.
        """
        self.phase_mode = phase_mode

    @kernel
    def write16(self, addr: TInt32, data: TInt32):
        """Write to 16-bit register.

        :param addr: Register address
        :param data: Data to be written
        """
        self.bus.set_config_mu(urukul.SPI_CONFIG | spi.SPI_END, 24,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write((addr << 24) | ((data & 0xffff) << 8))

    @kernel
    def write32(self, addr: TInt32, data: TInt32):
        """Write to 32-bit register.

        :param addr: Register address
        :param data: Data to be written
        """
        self.bus.set_config_mu(urukul.SPI_CONFIG, 8,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write(addr << 24)
        self.bus.set_config_mu(urukul.SPI_CONFIG | spi.SPI_END, 32,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write(data)

    @kernel
    def read16(self, addr: TInt32) -> TInt32:
        """Read from 16-bit register.

        :param addr: Register address
        """
        self.bus.set_config_mu(urukul.SPI_CONFIG, 8,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write((addr | 0x80) << 24)
        self.bus.set_config_mu(
            urukul.SPI_CONFIG | spi.SPI_END | spi.SPI_INPUT,
            16, urukul.SPIT_DDS_RD, self.chip_select)
        self.bus.write(0)
        return self.bus.read()

    @kernel
    def read32(self, addr: TInt32) -> TInt32:
        """Read from 32-bit register.

        :param addr: Register address
        """
        self.bus.set_config_mu(urukul.SPI_CONFIG, 8,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write((addr | 0x80) << 24)
        self.bus.set_config_mu(
            urukul.SPI_CONFIG | spi.SPI_END | spi.SPI_INPUT,
            32, urukul.SPIT_DDS_RD, self.chip_select)
        self.bus.write(0)
        return self.bus.read()

    @kernel
    def read64(self, addr: TInt32) -> TInt64:
        """Read from 64-bit register.

        :param addr: Register address
        :return: 64-bit integer register value
        """
        self.bus.set_config_mu(
            urukul.SPI_CONFIG, 8,
            urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write((addr | 0x80) << 24)
        self.bus.set_config_mu(
            urukul.SPI_CONFIG | spi.SPI_INPUT, 32,
            urukul.SPIT_DDS_RD, self.chip_select)
        self.bus.write(0)
        self.bus.set_config_mu(
            urukul.SPI_CONFIG | spi.SPI_END | spi.SPI_INPUT, 32,
            urukul.SPIT_DDS_RD, self.chip_select)
        self.bus.write(0)
        hi = self.bus.read()
        lo = self.bus.read()
        return (int64(hi) << 32) | lo

    @kernel
    def write64(self, addr: TInt32, data_high: TInt32, data_low: TInt32):
        """Write to 64-bit register.

        :param addr: Register address
        :param data_high: High (MSB) 32 data bits 
        :param data_low: Low (LSB) 32 data bits
        """
        self.bus.set_config_mu(urukul.SPI_CONFIG, 8,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write(addr << 24)
        self.bus.set_config_mu(urukul.SPI_CONFIG, 32,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write(data_high)
        self.bus.set_config_mu(urukul.SPI_CONFIG | spi.SPI_END, 32,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write(data_low)

    @kernel
    def write_ram(self, data: TList(TInt32)):
        """Write data to RAM.

        The profile to write to and the step, start, and end address
        need to be configured in advance and separately using
        :meth:`set_profile_ram` and the parent CPLD
        :meth:`~artiq.coredevice.urukul.ProtoRev9.set_profile`.

        :param data: Data to be written to RAM.
        """
        self.bus.set_config_mu(urukul.SPI_CONFIG, 8, urukul.SPIT_DDS_WR,
                               self.chip_select)
        self.bus.write(_AD9910_REG_RAM << 24)
        self.bus.set_config_mu(urukul.SPI_CONFIG, 32,
                               urukul.SPIT_DDS_WR, self.chip_select)
        for i in range(len(data) - 1):
            self.bus.write(data[i])
        self.bus.set_config_mu(urukul.SPI_CONFIG | spi.SPI_END, 32,
                               urukul.SPIT_DDS_WR, self.chip_select)
        self.bus.write(data[len(data) - 1])

    @kernel
    def read_ram(self, data: TList(TInt32)):
        """Read data from RAM.

        The profile to read from and the step, start, and end address
        need to be configured before and separately using
        :meth:`set_profile_ram` and the parent CPLD 
        :meth:`~artiq.coredevice.urukul.ProtoRev9.set_profile`.

        :param data: List to be filled with data read from RAM.
        """
        self.bus.set_config_mu(urukul.SPI_CONFIG, 8, urukul.SPIT_DDS_WR,
                               self.chip_select)
        self.bus.write((_AD9910_REG_RAM | 0x80) << 24)
        n = len(data) - 1
        if n > 0:
            self.bus.set_config_mu(urukul.SPI_CONFIG | spi.SPI_INPUT, 32,
                                   urukul.SPIT_DDS_RD, self.chip_select)
        preload = min(n, 8)
        for i in range(n):
            self.bus.write(0)
            if i >= preload:
                data[i - preload] = self.bus.read()
        self.bus.set_config_mu(
            urukul.SPI_CONFIG | spi.SPI_INPUT | spi.SPI_END, 32,
            urukul.SPIT_DDS_RD, self.chip_select)
        self.bus.write(0)
        for i in range(preload + 1):
            data[(n - preload) + i] = self.bus.read()

    @kernel
    def set_cfr1(self,
                 power_down: TInt32 = 0b0000,
                 phase_autoclear: TInt32 = 0,
                 drg_load_lrr: TInt32 = 0,
                 drg_autoclear: TInt32 = 0,
                 phase_clear: TInt32 = 0,
                 internal_profile: TInt32 = 0,
                 ram_destination: TInt32 = 0,
                 ram_enable: TInt32 = 0,
                 manual_osk_external: TInt32 = 0,
                 osk_enable: TInt32 = 0,
                 select_auto_osk: TInt32 = 0):
        """Set CFR1. See the AD9910 datasheet for parameter meanings and sizes.

        This method does not pulse ``IO_UPDATE.``

        :param power_down: Power down bits.
        :param phase_autoclear: Autoclear phase accumulator.
        :param phase_clear: Asynchronous, static reset of the phase accumulator.
        :param drg_load_lrr: Load digital ramp generator LRR.
        :param drg_autoclear: Autoclear digital ramp generator.
        :param internal_profile: Internal profile control.
        :param ram_destination: RAM destination
            (:const:`RAM_DEST_FTW`, :const:`RAM_DEST_POW`,
            :const:`RAM_DEST_ASF`, :const:`RAM_DEST_POWASF`).
        :param ram_enable: RAM mode enable.
        :param manual_osk_external: Enable OSK pin control in manual OSK mode.
        :param osk_enable: Enable OSK mode.
        :param select_auto_osk: Select manual or automatic OSK mode.
        """
        self.write32(_AD9910_REG_CFR1,
                     (ram_enable << 31) |
                     (ram_destination << 29) |
                     (manual_osk_external << 23) |
                     (internal_profile << 17) |
                     (drg_load_lrr << 15) |
                     (drg_autoclear << 14) |
                     (phase_autoclear << 13) |
                     (phase_clear << 11) |
                     (osk_enable << 9) |
                     (select_auto_osk << 8) |
                     (power_down << 4) |
                     2)  # SDIO input only, MSB first

    @kernel
    def set_cfr2(self, 
                 asf_profile_enable: TInt32 = 1, 
                 drg_destination: TInt32 = 0,
                 drg_enable: TInt32 = 0, 
                 drg_nodwell_high: TInt32 = 0,
                 drg_nodwell_low: TInt32 = 0,
                 effective_ftw: TInt32 = 1,
                 sync_validation_disable: TInt32 = 0, 
                 matched_latency_enable: TInt32 = 0):
        """Set CFR2. See the AD9910 datasheet for parameter meanings and sizes.

        This method does not pulse ``IO_UPDATE``.

        :param asf_profile_enable: Enable amplitude scale from single tone profiles.
        :param drg_destination: Digital ramp destination. Determines the parameter to modulate:
            * 0: Frequency
            * 1: Phase
            * 2: Amplitude
        :param drg_enable: Digital ramp enable.
        :param drg_nodwell_high: Digital ramp no-dwell high.
        :param drg_nodwell_low: Digital ramp no-dwell low.
        :param effective_ftw: Read effective FTW.
        :param sync_validation_disable: Disable the SYNC_SMP_ERR pin indicating
            (active high) detection of a synchronization pulse sampling error.
        :param matched_latency_enable: Control the application timing of amplitude,
            phase, and frequency changes at the DDS output:
            * 0: Changes are applied in the order listed.
            * 1: Changes are applied simultaneously.
        """
        self.write32(_AD9910_REG_CFR2,
                     (asf_profile_enable << 24) |
                     (drg_destination << 20) |
                     (drg_enable << 19) |
                     (drg_nodwell_high << 18) |
                     (drg_nodwell_low << 17) |
                     (effective_ftw << 16) |
                     (matched_latency_enable << 7) |
                     (sync_validation_disable << 5))

    @kernel
    def init(self, blind: TBool = False):
        """Initialize and configure the DDS.

        Sets up SPI mode, confirms chip presence, powers down unused blocks,
        configures the PLL, waits for PLL lock. Uses the ``IO_UPDATE`` 
        signal multiple times.

        :param blind: Do not read back DDS identity and do not wait for lock.
        """
        self.sync_data.init()
        if self.sync_data.sync_delay_seed >= 0 and not self.cpld.sync_div:
            raise ValueError("parent CPLD does not drive SYNC")
        if self.sync_data.sync_delay_seed >= 0:
            if self.sysclk_per_mu != self.sysclk * self.core.ref_period:
                raise ValueError("incorrect clock ratio for synchronization")
        delay(50 * ms)  # slack

        # Set SPI mode
        self.set_cfr1()
        self.io_update.pulse(1 * ms)
        delay(1 * ms)
        if not blind:
            # Use the AUX DAC setting to identify and confirm presence
            aux_dac = self.read32(_AD9910_REG_AUX_DAC)
            if aux_dac & 0xff != 0x7f:
                raise ValueError("Urukul AD9910 AUX_DAC mismatch")
            delay(50 * us)  # slack
        # Configure PLL settings and bring up PLL
        # enable amplitude scale from profiles
        # read effective FTW
        # sync timing validation disable (enabled later)
        self.set_cfr2(sync_validation_disable=1)
        self.io_update.pulse(1 * ms)
        cfr3 = (0x0807c000 | (self.pll_vco << 24) |
                (self.pll_cp << 19) | (self.pll_en << 8) |
                (self.pll_n << 1))
        self.write32(_AD9910_REG_CFR3, cfr3 | 0x400)  # PFD reset
        self.io_update.pulse(1 * us)
        if self.pll_en:
            self.write32(_AD9910_REG_CFR3, cfr3)
            self.io_update.pulse(1 * ms)
            if blind:
                delay(100 * ms)
            else:
                # Wait for PLL lock, up to 100 ms
                for i in range(100):
                    sta = self.cpld.sta_read()
                    lock = urukul_sta_pll_lock(sta)
                    delay(1 * ms)
                    if lock & (1 << self.chip_select - 4):
                        break
                    if i >= 100 - 1:
                        raise ValueError("PLL lock timeout")
        delay(10 * us)  # slack
        if self.sync_data.sync_delay_seed >= 0 and not blind:
            self.tune_sync_delay(self.sync_data.sync_delay_seed)
        delay(1 * ms)
        # FIXME: Re-write the configuration (needed for proper
        # initialization when using _RegIOUpdate).
        self.cpld.cfg_write(self.cpld.cfg_reg)

    @kernel
    def power_down(self, bits: TInt32 = 0b1111):
        """Power down DDS.

        :param bits: Power-down bits, see datasheet
        """
        self.set_cfr1(power_down=bits)
        self.io_update.pulse(1 * us)

    @kernel
    def set_mu(self, ftw: TInt32 = 0, pow_: TInt32 = 0, asf: TInt32 = 0x3fff,
               phase_mode: TInt32 = _PHASE_MODE_DEFAULT,
               ref_time_mu: TInt64 = int64(-1),
               profile: TInt32 = DEFAULT_PROFILE,
               ram_destination: TInt32 = -1) -> TInt32:
        """Set DDS data in machine units.

        This uses machine units (FTW, POW, ASF). The frequency tuning word
        width is 32, the phase offset word width is 16, and the amplitude
        scale factor width is 14.

        After the SPI transfer, the shared IO update pin is pulsed to
        activate the data.

        .. seealso:: :meth:`AD9910.set_phase_mode` for a definition of the different
            phase modes.

        .. warning::
            Deterministic phase control depends on correct alignment of operations
            to a 4ns grid (``SYNC_CLK``). This function uses :meth:`~artiq.language.core.now_mu()`
            to ensure such alignment automatically. When replayed over DMA, however, the ensuing
            event sequence *must* be started at the same offset relative to ``SYNC_CLK``, or
            unstable ``SYNC_CLK`` cycle assignment (i.e. inconsistent delays of exactly 4ns) will
            result.

        :param ftw: Frequency tuning word: 32-bit.
        :param pow_: Phase tuning word: 16-bit unsigned.
        :param asf: Amplitude scale factor: 14-bit unsigned.
        :param phase_mode: If specified, overrides the default phase mode set
            by :meth:`set_phase_mode` for this call.
        :param ref_time_mu: Fiducial time used to compute absolute or tracking
            phase updates. In machine units as obtained by :meth:`~artiq.language.core.now_mu()`.
        :param profile: Single tone profile number to set (0-7, default: 7).
            Ineffective if ``ram_destination`` is specified.
        :param ram_destination: RAM destination (:const:`RAM_DEST_FTW`,
            :const:`RAM_DEST_POW`, :const:`RAM_DEST_ASF`,
            :const:`RAM_DEST_POWASF`). If specified, write free DDS parameters
            to the ASF/FTW/POW registers instead of to the single tone profile
            register (default behaviour, see ``profile``).
        :return: Resulting phase offset word after application of phase
            tracking offset. When using :const:`PHASE_MODE_CONTINUOUS` in
            subsequent calls, use this value as the "current" phase.
        """
        if phase_mode == _PHASE_MODE_DEFAULT:
            phase_mode = self.phase_mode
        # Align to coarse RTIO which aligns SYNC_CLK. I.e. clear fine TSC
        # This will not cause a collision or sequence error.
        at_mu(now_mu() & ~7)
        if phase_mode != PHASE_MODE_CONTINUOUS:
            # Auto-clear phase accumulator on IO_UPDATE.
            # This is active already for the next IO_UPDATE
            self.set_cfr1(phase_autoclear=1)
            if phase_mode == PHASE_MODE_TRACKING and ref_time_mu < 0:
                # set default fiducial time stamp
                ref_time_mu = 0
            if ref_time_mu >= 0:
                # 32 LSB are sufficient.
                # Also no need to use IO_UPDATE time as this
                # is equivalent to an output pipeline latency.
                dt = int32(now_mu()) - int32(ref_time_mu)
                pow_ += dt * ftw * self.sysclk_per_mu >> 16
        if ram_destination == -1:
            self.write64(_AD9910_REG_PROFILE0 + profile,
                         (asf << 16) | (pow_ & 0xffff), ftw)
        else:
            if not ram_destination == RAM_DEST_FTW:
                self.set_ftw(ftw)
            if not ram_destination == RAM_DEST_POWASF:
                if not ram_destination == RAM_DEST_ASF:
                    self.set_asf(asf)
                if not ram_destination == RAM_DEST_POW:
                    self.set_pow(pow_)
        delay_mu(int64(self.sync_data.io_update_delay))
        self.io_update.pulse_mu(8)  # assumes 8 mu > t_SYN_CCLK
        at_mu(now_mu() & ~7)  # clear fine TSC again
        if phase_mode != PHASE_MODE_CONTINUOUS:
            self.set_cfr1()
            # future IO_UPDATE will activate
        return pow_

    @kernel
    def get_mu(self, profile: TInt32 = DEFAULT_PROFILE
               ) -> TTuple([TInt32, TInt32, TInt32]):
        """Get the frequency tuning word, phase offset word,
        and amplitude scale factor.

        See also :meth:`AD9910.get`.

        :param profile: Profile number to get (0-7, default: 7)
        :return: A tuple (FTW, POW, ASF)
        """

        # Read data
        data = int64(self.read64(_AD9910_REG_PROFILE0 + profile))
        # Extract and return fields
        ftw = int32(data)
        pow_ = int32((data >> 32) & 0xffff)
        asf = int32((data >> 48) & 0x3fff)
        return ftw, pow_, asf

    @kernel
    def set_profile_ram(self, start: TInt32, end: TInt32, step: TInt32 = 1,
                        profile: TInt32 = _DEFAULT_PROFILE_RAM,
                        nodwell_high: TInt32 = 0, zero_crossing: TInt32 = 0,
                        mode: TInt32 = 1):
        """Set the RAM profile settings. See also AD9910 datasheet.

        :param start: Profile start address in RAM (10-bit).
        :param end: Profile end address in RAM, inclusive (10-bit).
        :param step: Profile time step, counted in DDS sample clock
            cycles, typically 4 ns (16-bit, default: 1)
        :param profile: Profile index (0 to 7) (default: 0).
        :param nodwell_high: No-dwell high bit (default: 0,
            see AD9910 documentation).
        :param zero_crossing: Zero crossing bit (default: 0,
            see AD9910 documentation).
        :param mode: Profile RAM mode (:const:`RAM_MODE_DIRECTSWITCH`,
            :const:`RAM_MODE_RAMPUP`, :const:`RAM_MODE_BIDIR_RAMP`,
            :const:`RAM_MODE_CONT_BIDIR_RAMP`, or
            :const:`RAM_MODE_CONT_RAMPUP`, default:
            :const:`RAM_MODE_RAMPUP`)
        """
        hi = (step << 8) | (end >> 2)
        lo = ((end << 30) | (start << 14) | (nodwell_high << 5) |
              (zero_crossing << 3) | mode)
        self.write64(_AD9910_REG_PROFILE0 + profile, hi, lo)

    @kernel
    def set_ftw(self, ftw: TInt32):
        """Set the value stored to the AD9910's frequency tuning word (FTW)
        register.

        :param ftw: Frequency tuning word to be stored, range: 0 to 0xffffffff.
        """
        self.write32(_AD9910_REG_FTW, ftw)

    @kernel
    def set_asf(self, asf: TInt32):
        """Set the value stored to the AD9910's amplitude scale factor (ASF)
        register.

        :param asf: Amplitude scale factor to be stored, range: 0 to 0x3fff.
        """
        self.write32(_AD9910_REG_ASF, asf << 2)

    @kernel
    def set_pow(self, pow_: TInt32):
        """Set the value stored to the AD9910's phase offset word (POW)
        register.

        :param pow_: Phase offset word to be stored, range: 0 to 0xffff.
        """
        self.write16(_AD9910_REG_POW, pow_)

    @kernel
    def get_ftw(self) -> TInt32:
        """Get the value stored to the AD9910's frequency tuning word (FTW)
        register.

        :return: Frequency tuning word
        """
        return self.read32(_AD9910_REG_FTW)

    @kernel
    def get_asf(self) -> TInt32:
        """Get the value stored to the AD9910's amplitude scale factor (ASF)
        register.

        :return: Amplitude scale factor
        """
        return self.read32(_AD9910_REG_ASF) >> 2

    @kernel
    def get_pow(self) -> TInt32:
        """Get the value stored to the AD9910's phase offset word (POW)
        register.

        :return: Phase offset word
        """
        return self.read16(_AD9910_REG_POW)

    @portable(flags={"fast-math"})
    def frequency_to_ftw(self, frequency: TFloat) -> TInt32:
        """Return the 32-bit frequency tuning word corresponding to the given
        frequency.
        """
        return int32(round(self.ftw_per_hz * frequency))

    @portable(flags={"fast-math"})
    def ftw_to_frequency(self, ftw: TInt32) -> TFloat:
        """Return the frequency corresponding to the given frequency tuning
        word.
        """
        return ftw / self.ftw_per_hz

    @portable(flags={"fast-math"})
    def turns_to_pow(self, turns: TFloat) -> TInt32:
        """Return the 16-bit phase offset word corresponding to the given phase
        in turns."""
        return int32(round(turns * 0x10000)) & int32(0xffff)

    @portable(flags={"fast-math"})
    def pow_to_turns(self, pow_: TInt32) -> TFloat:
        """Return the phase in turns corresponding to a given phase offset
        word."""
        return pow_ / 0x10000

    @portable(flags={"fast-math"})
    def amplitude_to_asf(self, amplitude: TFloat) -> TInt32:
        """Return 14-bit amplitude scale factor corresponding to given
        fractional amplitude."""
        code = int32(round(amplitude * 0x3fff))
        if code < 0 or code > 0x3fff:
            raise ValueError("Invalid AD9910 fractional amplitude!")
        return code

    @portable(flags={"fast-math"})
    def asf_to_amplitude(self, asf: TInt32) -> TFloat:
        """Return amplitude as a fraction of full scale corresponding to given
        amplitude scale factor."""
        return asf / float(0x3fff)

    @portable(flags={"fast-math"})
    def frequency_to_ram(self, frequency: TList(TFloat), ram: TList(TInt32)):
        """Convert frequency values to RAM profile data.

        To be used with :const:`RAM_DEST_FTW`.

        :param frequency: List of frequency values in Hz.
        :param ram: List to write RAM data into.
            Suitable for :meth:`write_ram`.
        """
        for i in range(len(ram)):
            ram[i] = self.frequency_to_ftw(frequency[i])

    @portable(flags={"fast-math"})
    def turns_to_ram(self, turns: TList(TFloat), ram: TList(TInt32)):
        """Convert phase values to RAM profile data.

        To be used with :const:`RAM_DEST_POW`.

        :param turns: List of phase values in turns.
        :param ram: List to write RAM data into.
            Suitable for :meth:`write_ram`.
        """
        for i in range(len(ram)):
            ram[i] = self.turns_to_pow(turns[i]) << 16

    @portable(flags={"fast-math"})
    def amplitude_to_ram(self, amplitude: TList(TFloat), ram: TList(TInt32)):
        """Convert amplitude values to RAM profile data.

        To be used with :const:`RAM_DEST_ASF`.

        :param amplitude: List of amplitude values in units of full scale.
        :param ram: List to write RAM data into.
            Suitable for :meth:`write_ram`.
        """
        for i in range(len(ram)):
            ram[i] = self.amplitude_to_asf(amplitude[i]) << 18

    @portable(flags={"fast-math"})
    def turns_amplitude_to_ram(self, turns: TList(TFloat),
                               amplitude: TList(TFloat), ram: TList(TInt32)):
        """Convert phase and amplitude values to RAM profile data.

        To be used with :const:`RAM_DEST_POWASF`.

        :param turns: List of phase values in turns.
        :param amplitude: List of amplitude values in units of full scale.
        :param ram: List to write RAM data into.
            Suitable for :meth:`write_ram`.
        """
        for i in range(len(ram)):
            ram[i] = ((self.turns_to_pow(turns[i]) << 16) |
                      self.amplitude_to_asf(amplitude[i]) << 2)

    @kernel
    def set_frequency(self, frequency: TFloat):
        """Set the value stored to the AD9910's frequency tuning word (FTW)
        register.

        :param frequency: frequency to be stored, in Hz.
        """
        self.set_ftw(self.frequency_to_ftw(frequency))

    @kernel
    def set_amplitude(self, amplitude: TFloat):
        """Set the value stored to the AD9910's amplitude scale factor (ASF)
        register.

        :param amplitude: amplitude to be stored, in units of full scale.
        """
        self.set_asf(self.amplitude_to_asf(amplitude))

    @kernel
    def set_phase(self, turns: TFloat):
        """Set the value stored to the AD9910's phase offset word (POW)
        register.

        :param turns: phase offset to be stored, in turns.
        """
        self.set_pow(self.turns_to_pow(turns))

    @kernel
    def get_frequency(self) -> TFloat:
        """Get the value stored to the AD9910's frequency tuning word (FTW)
        register.

        :return: frequency in Hz.
        """
        return self.ftw_to_frequency(self.get_ftw())

    @kernel
    def get_amplitude(self) -> TFloat:
        """Get the value stored to the AD9910's amplitude scale factor (ASF)
        register.

        :return: amplitude in units of full scale.
        """
        return self.asf_to_amplitude(self.get_asf())

    @kernel
    def get_phase(self) -> TFloat:
        """Get the value stored to the AD9910's phase offset word (POW)
        register.

        :return: phase offset in turns.
        """
        return self.pow_to_turns(self.get_pow())

    @kernel
    def set(self, frequency: TFloat = 0.0, phase: TFloat = 0.0,
            amplitude: TFloat = 1.0, phase_mode: TInt32 = _PHASE_MODE_DEFAULT,
            ref_time_mu: TInt64 = int64(-1), profile: TInt32 = DEFAULT_PROFILE,
            ram_destination: TInt32 = -1) -> TFloat:
        """Set DDS data in SI units.

        See also :meth:`AD9910.set_mu`.

        :param frequency: Frequency in Hz
        :param phase: Phase tuning word in turns
        :param amplitude: Amplitude in units of full scale
        :param phase_mode: Phase mode constant
        :param ref_time_mu: Fiducial time stamp in machine units
        :param profile: Single tone profile to affect.
        :param ram_destination: RAM destination.
        :return: Resulting phase offset in turns
        """
        return self.pow_to_turns(self.set_mu(
            self.frequency_to_ftw(frequency), self.turns_to_pow(phase),
            self.amplitude_to_asf(amplitude), phase_mode, ref_time_mu,
            profile, ram_destination))

    @kernel
    def get(self, profile: TInt32 = DEFAULT_PROFILE
            ) -> TTuple([TFloat, TFloat, TFloat]):
        """Get the frequency, phase, and amplitude.

        See also :meth:`AD9910.get_mu`.

        :param profile: Profile number to get (0-7, default: 7)
        :return: A tuple (frequency, phase, amplitude)
        """

        # Get values
        ftw, pow_, asf = self.get_mu(profile)
        # Convert and return
        return (self.ftw_to_frequency(ftw), self.pow_to_turns(pow_),
                self.asf_to_amplitude(asf))

    @kernel
    def set_att_mu(self, att: TInt32):
        """Set digital step attenuator in machine units.

        This method will write the attenuator settings of this channel
        (For Urukul proto_rev 0x08, all four channels will be updated at same time).
        See also
        :meth:`CPLD.get_channel_att <artiq.coredevice.urukul.CPLD.set_att_mu>`.

        :param att: Attenuation setting, 8-bit digital.
        """
        self.cpld.set_att_mu(self.chip_select - 4, att)

    @kernel
    def set_att(self, att: TFloat):
        """Set digital step attenuator in SI units.

        This method will write the attenuator settings of this channel
        (For Urukul proto_rev 0x08, all four channels will be updated at same time).
        See also
        :meth:`CPLD.get_channel_att <artiq.coredevice.urukul.CPLD.set_att>`.

        :param att: Attenuation in dB.
        """
        self.cpld.set_att(self.chip_select - 4, att)

    @kernel
    def get_att_mu(self) -> TInt32:
        """Get digital step attenuator value in machine units. See also
        :meth:`CPLD.get_channel_att <artiq.coredevice.urukul.CPLD.get_channel_att_mu>`.

        :return: Attenuation setting, 8-bit digital.
        """
        return self.cpld.get_channel_att_mu(self.chip_select - 4)

    @kernel
    def get_att(self) -> TFloat:
        """Get digital step attenuator value in SI units. See also 
        :meth:`CPLD.get_channel_att <artiq.coredevice.urukul.CPLD.get_channel_att>`.

        :return: Attenuation in dB.
        """
        return self.cpld.get_channel_att(self.chip_select - 4)

    @kernel
    def cfg_sw(self, state: TBool):
        """Set CPLD CFG RF switch state. The RF switch is controlled by the
        logical OR of the CPLD configuration shift register
        RF switch bit and the SW TTL line (if used).

        :param state: CPLD CFG RF switch bit
        """
        self.cpld.cfg_sw(self.chip_select - 4, state)

    @kernel
    def cfg_osk(self, state: TBool):
        """Set CPLD CFG OSK state.

        :param state: CPLD CFG OSK bit
        """
        self.cpld.cfg_osk(self.chip_select - 4, state)

    @kernel
    def cfg_drctl(self, state: TBool):
        """Set CPLD CFG DRCTL state.

        :param state: CPLD CFG DRCTL bit
        """
        self.cpld.cfg_drctl(self.chip_select - 4, state)

    @kernel
    def cfg_drhold(self, state: TBool):
        """Set CPLD CFG DRHOLD state.

        :param state: CPLD CFG DRHOLD bit
        """
        self.cpld.cfg_drhold(self.chip_select - 4, state)

    @kernel
    def cfg_mask_nu(self, state: TBool):
        """Set CPLD CFG MASK_NU state.

        :param state: CPLD CFG MASK_NU bit
        """
        self.cpld.cfg_mask_nu(self.chip_select - 4, state)

    @kernel
    def cfg_att_en(self, state: TBool):
        """Set CPLD CFG ATT_EN state.

        :param state: CPLD CFG ATT_EN bit
        """
        self.cpld.cfg_att_en(self.chip_select - 4, state)

    @kernel
    def set_profile(self, profile: TInt32):
        """Set the PROFILE pins.

        .. warning::
            With CPLD proto_rev 8, this will silently set the profile pins of
            all other AD9910 DDS channels on the same board!

            Use proto_rev 9 for channel-specific profile setting.

        :param profile: PROFILE pins in numeric representation (0-7).
        """
        self.cpld.set_profile(self.chip_select - 4, profile)

    @kernel
    def set_sync(self, 
                 in_delay: TInt32, 
                 window: TInt32, 
                 en_sync_gen: TInt32 = 0):
        """Set the relevant parameters in the multi device synchronization
        register. See the AD9910 datasheet for details. The ``SYNC`` clock
        generator preset value is set to zero, and the ``SYNC_OUT`` generator is
        disabled by default.

        :param in_delay: ``SYNC_IN`` delay tap (0-31) in steps of ~75ps
        :param window: Symmetric ``SYNC_IN`` validation window (0-15) in
            steps of ~75ps for both hold and setup margin.
        :param en_sync_gen: Whether to enable the DDS-internal sync generator
            (``SYNC_OUT``, cf. ``sync_sel == 1``). Should be left off for the normal
            use case, where the ``SYNC`` clock is supplied by the core device.
        """
        self.write32(_AD9910_REG_SYNC,
                     (window << 28) |  # SYNC S/H validation delay
                     (1 << 27) |  # SYNC receiver enable
                     (en_sync_gen << 26) |  # SYNC generator enable
                     (0 << 25) |  # SYNC generator SYS rising edge
                     (0 << 18) |  # SYNC preset
                     (0 << 11) |  # SYNC output delay
                     (in_delay << 3))  # SYNC receiver delay

    @kernel
    def clear_smp_err(self):
        """Clear the ``SMP_ERR`` flag and enables ``SMP_ERR`` validity monitoring.

        Violations of the ``SYNC_IN`` sample and hold margins will result in
        SMP_ERR being asserted. This then also activates the red LED on
        the respective Urukul channel.

        Also modifies CFR2.
        """
        self.set_cfr2(sync_validation_disable=1)  # clear SMP_ERR
        self.io_update.pulse(1 * us)
        delay(10 * us)  # slack
        self.set_cfr2(sync_validation_disable=0)  # enable SMP_ERR
        self.io_update.pulse(1 * us)

    @kernel
    def tune_sync_delay(self,
                        search_seed: TInt32 = 15) -> TTuple([TInt32, TInt32]):
        """Find a stable ``SYNC_IN`` delay.

        This method first locates a valid ``SYNC_IN`` delay at zero validation
        window size (setup/hold margin) by scanning around `search_seed`. It
        then looks for similar valid delays at successively larger validation
        window sizes until none can be found. It then decreases the validation
        window a bit to provide some slack and stability and returns the
        optimal values.

        This method and :meth:`tune_io_update_delay` can be run in any order.

        :param search_seed: Start value for valid ``SYNC_IN`` delay search.
            Defaults to 15 (half range).
        :return: Tuple of optimal delay and window size.
        """
        if not self.cpld.sync_div:
            raise ValueError("parent cpld does not drive SYNC")
        search_span = 13
        # FIXME https://github.com/sinara-hw/Urukul/issues/16
        # should both be 2-4 once kasli sync_in jitter is identified
        min_window = 0
        margin = 1  # 1*75ps setup and hold
        for window in range(16):
            next_seed = -1
            for in_delay in range(search_span - 2 * window):
                # alternate search direction around search_seed
                if in_delay & 1:
                    in_delay = -in_delay
                in_delay = search_seed + (in_delay >> 1)
                if in_delay < 0 or in_delay > 31:
                    continue
                self.set_sync(in_delay, window)
                self.clear_smp_err()
                # integrate SMP_ERR statistics for a few hundred cycles
                delay(100 * us)
                err = urukul_sta_smp_err(self.cpld.sta_read())
                delay(100 * us)  # slack
                if not (err >> (self.chip_select - 4)) & 1:
                    next_seed = in_delay
                    break
            if next_seed >= 0:  # valid delay found, scan next window
                search_seed = next_seed
                continue
            elif window > min_window:
                # no valid delay found here, roll back and add margin
                window = max(min_window, window - 1 - margin)
                self.set_sync(search_seed, window)
                self.clear_smp_err()
                delay(100 * us)  # slack
                return search_seed, window
            else:
                break
        raise ValueError("no valid window/delay")

    @kernel
    def measure_io_update_alignment(self, delay_start: TInt64,
                                    delay_stop: TInt64) -> TInt32:
        """Use the digital ramp generator to locate the alignment between
        ``IO_UPDATE`` and ``SYNC_CLK``.

        The ramp generator is set up to a linear frequency ramp
        ``(dFTW/t_SYNC_CLK=1)`` and started at a coarse RTIO time stamp plus
        ``delay_start`` and stopped at a coarse RTIO time stamp plus
        ``delay_stop``.

        :param delay_start: Start ``IO_UPDATE`` delay in machine units.
        :param delay_stop: Stop ``IO_UPDATE`` delay in machine units.
        :return: Odd/even ``SYNC_CLK`` cycle indicator.
        """
        # set up DRG
        self.set_cfr1(drg_load_lrr=1, drg_autoclear=1)
        # DRG -> FTW, DRG enable
        self.set_cfr2(drg_enable=1)
        # no limits
        self.write64(_AD9910_REG_RAMP_LIMIT, -1, 0)
        # DRCTL=0, dt=1 t_SYNC_CLK
        self.write32(_AD9910_REG_RAMP_RATE, 0x00010000)
        # dFTW = 1, (work around negative slope)
        self.write64(_AD9910_REG_RAMP_STEP, -1, 0)
        # delay io_update after RTIO edge
        t = now_mu() + 8 & ~7
        at_mu(t + delay_start)
        # assumes a maximum t_SYNC_CLK period
        self.io_update.pulse_mu(16 - delay_start)  # realign
        # disable DRG autoclear and LRR on io_update
        self.set_cfr1()
        # stop DRG
        self.write64(_AD9910_REG_RAMP_STEP, 0, 0)
        at_mu(t + 0x1000 + delay_stop)
        self.io_update.pulse_mu(16 - delay_stop)  # realign
        ftw = self.read32(_AD9910_REG_FTW)  # read out effective FTW
        delay(100 * us)  # slack
        # disable DRG
        self.set_cfr2(drg_enable=0)
        self.io_update.pulse_mu(8)
        return ftw & 1

    @kernel
    def tune_io_update_delay(self) -> TInt32:
        """Find a stable ``IO_UPDATE`` delay alignment.

        Scan through increasing ``IO_UPDATE`` delays until a delay is found that
        lets ``IO_UPDATE`` be registered in the next ``SYNC_CLK`` cycle. Return a
        ``IO_UPDATE`` delay that is as far away from that ``SYNC_CLK`` edge
        as possible.

        This method assumes that the ``IO_UPDATE`` TTLOut device has one machine
        unit resolution (SERDES).

        This method and :meth:`tune_sync_delay` can be run in any order.

        :return: Stable ``IO_UPDATE`` delay to be passed to the constructor
            :class:`AD9910` via the device database.
        """
        period = self.sysclk_per_mu * 4  # SYNC_CLK period
        repeat = 100
        for i in range(period):
            t = 0
            # check whether the sync edge is strictly between i, i+2
            for j in range(repeat):
                t += self.measure_io_update_alignment(i, i + 2)
            if t != 0:  # no certain edge
                continue
            # check left/right half: i,i+1 and i+1,i+2
            t1 = [0, 0]
            for j in range(repeat):
                t1[0] += self.measure_io_update_alignment(i, i + 1)
                t1[1] += self.measure_io_update_alignment(i + 1, i + 2)
            if ((t1[0] == 0 and t1[1] == 0) or
                    (t1[0] == repeat and t1[1] == repeat)):
                # edge is not close to i + 1, can't interpret result
                raise ValueError(
                    "no clear IO_UPDATE-SYNC_CLK alignment edge found")
            else:
                # the good delay is period//2 after the edge
                return (i + 1 + period // 2) & (period - 1)
        raise ValueError("no IO_UPDATE-SYNC_CLK alignment edge found")

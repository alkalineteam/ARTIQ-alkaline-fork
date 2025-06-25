import logging
from artiq.experiment import EnvExperiment
from artiq.experiment import kernel
from artiq.experiment import delay
from artiq.coredevice.core import Core

logger = logging.getLogger(__name__)

class test_timing(EnvExperiment):

    def build(self):
        self.setattr_device("core")
        self.core: Core

    @kernel
    def run(self):
        self.core.reset()
        self.core.break_realtime()
        
        t1 = self.core.get_rtio_counter_mu()
        delay(500*ms)
        delay(-100*ms)
        t2 = self.core.get_rtio_counter_mu()
        print((t2-t1)*1e-6, "ms")

        print("Timing test is done")
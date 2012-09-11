# Copyright (C) 2010-2012 Cuckoo Sandbox Developers.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.

import os
import sys
import time
import shutil
import logging
from threading import Thread, Lock
from time import sleep

from lib.cuckoo.common.exceptions import CuckooAnalysisError, CuckooMachineError, CuckooGuestError, CuckooOperationalError
from lib.cuckoo.common.abstracts import Dictionary, MachineManager
from lib.cuckoo.common.utils import File, create_folders, create_folder
from lib.cuckoo.common.config import Config
from lib.cuckoo.core.database import Database
from lib.cuckoo.core.guest import GuestManager
from lib.cuckoo.core.screener import Screener
from lib.cuckoo.core.sniffer import Sniffer
from lib.cuckoo.core.processor import Processor
from lib.cuckoo.core.reporter import Reporter
from lib.cuckoo.common.constants import CUCKOO_ROOT

log = logging.getLogger(__name__)

mmanager = None
machine_lock = Lock()

class AnalysisManager(Thread):
    """Analysis manager thread."""

    def __init__(self, task):
        """@param task: task."""
        Thread.__init__(self)
        Thread.daemon = True
        self.task = task
        self.cfg = Config()
        self.analysis = Dictionary()

    def init_storage(self):
        """Initialize analyses storage folder.
        @raise CuckooAnalysisError: if storage folder already exists."""
        self.analysis.results_folder = os.path.join(os.path.join(CUCKOO_ROOT, "storage", "analyses"), str(self.task.id))

        if os.path.exists(self.analysis.results_folder):
            raise CuckooAnalysisError("Analysis results folder already exists at path \"%s\", analysis aborted" % self.analysis.results_folder)

        try:
            create_folder(folder=self.analysis.results_folder)
        except CuckooOperationalError:
            raise CuckooAnalysisError("Unable to create analysis folder %s" % self.analysis.results_folder)

    def store_file(self):
        """Store sample file.
        @raise CuckooAnalysisError: if unable to store file."""
        md5 = File(self.task.file_path).get_md5()
        self.analysis.stored_file_path = os.path.join(CUCKOO_ROOT, "storage", "binaries", md5)

        if os.path.exists(self.analysis.stored_file_path):
            log.info("File already exists at \"%s\"" % self.analysis.stored_file_path)
        else:
            try:
                shutil.copy(self.task.file_path, self.analysis.stored_file_path)
            except (IOError, shutil.Error) as e:
                raise CuckooAnalysisError("Unable to store file from \"%s\" to \"%s\", analysis aborted"
                                          % (self.task.file_path, self.analysis.stored_file_path))

        try:
            new_binary_path = os.path.join(self.analysis.results_folder, "binary")

            # On Windows systems, symlink is obviously not supported, therefore we'll just copy
            # the binary until we find a more efficient solution.
            if hasattr(os, "symlink"):
                os.symlink(self.analysis.stored_file_path, new_binary_path)
            else:
                shutil.copy(self.analysis.stored_file_path, new_binary_path)
        except (AttributeError, OSError) as e:
            raise CuckooAnalysisError("Unable to create symlink/copy from \"%s\" to \"%s\"" % (self.analysis.stored_file_path, self.analysis.results_folder))

        if self.cfg.cuckoo.delete_original:
            try:
                os.remove(self.task.file_path)
            except OSError as e:
                log.error("Unable to delete original file at path \"%s\": %s" % (self.task.file_path, e))

    def build_options(self):
        """Get analysis options.
        @return: options dict.
        """
        options = {}

        options["file_path"] = self.task.file_path
        options["package"] = self.task.package
        options["machine"] = self.task.machine
        options["platform"] = self.task.platform
        options["options"] = self.task.options
        options["custom"] = self.task.custom

        if not self.task.timeout or self.task.timeout == 0:
            options["timeout"] = self.cfg.cuckoo.analysis_timeout
        else:
            options["timeout"] = self.task.timeout

        options["file_name"] = File(self.task.file_path).get_name()
        options["file_type"] = File(self.task.file_path).get_type()
        options["started"] = time.time()

        return options

    def launch_analysis(self):
        """Start analysis.
        @raise CuckooAnalysisError: if unable to start analysis.
        """
        log.info("Starting analysis of file \"%s\" (task=%s)" % (self.task.file_path, self.task.id))

        if not os.path.exists(self.task.file_path):
            raise CuckooAnalysisError("The file to analyze does not exist at path \"%s\", analysis aborted" % self.task.file_path)

        self.init_storage()
        self.store_file()
        options = self.build_options()
        
        while True:
            machine_lock.acquire()
            vm = mmanager.acquire(machine_id=self.task.machine, platform=self.task.platform)
            machine_lock.release()
            if not vm:
                log.debug("Task #%s: no machine available" % self.task.id)
                time.sleep(1)
            else:
                log.info("Task #%s: acquired machine %s (label=%s)" % (self.task.id, vm.id, vm.label))
                break

        # Initialize sniffer
        if self.cfg.cuckoo.use_sniffer:
            sniffer = Sniffer(self.cfg.cuckoo.tcpdump)
            sniffer.start(interface=self.cfg.cuckoo.interface, host=vm.ip, file_path=os.path.join(self.analysis.results_folder, "dump.pcap"))
        else:
            sniffer = False

        # Initialize VMWare ScreenShot
        MachineManager()
        module = MachineManager.__subclasses__()[0]
        mman = module()
        mman_conf = os.path.join(CUCKOO_ROOT, "conf", "%s.conf" % self.cfg.cuckoo.machine_manager)
        if not os.path.exists(mman_conf):
            raise CuckooMachineError("The configuration file for machine manager \"%s\" does not exist at path: %s"
                                     % (self.cfg.cuckoo.machine_manager, mman_conf))
        mman.set_options(Config(mman_conf))
        mman.initialize(self.cfg.cuckoo.machine_manager)
        screener = Screener(mman.options.vmware.path, vm.label, "avtest", "avtest", self.analysis.results_folder)
        
        try:
            # Start machine
            mmanager.start(vm.label)
            # Initialize guest manager
            guest = GuestManager(vm.id, vm.ip, vm.platform)
            # Launch analysis
            guest.start_analysis(options)
            # Start Screenshots
            screener.start()
            # Wait for analysis to complete
            success = guest.wait_for_completion()
            # Stop sniffer
            if sniffer:
                sniffer.stop()
            # Stop Screenshots
            if screener:
                screener.stop()
            if not success:
                raise CuckooAnalysisError("Task #%s: analysis failed, review previous errors" % self.task.id)
            # Save results
            guest.save_results(self.analysis.results_folder)
        except (CuckooMachineError, CuckooGuestError) as e:
            raise CuckooAnalysisError(e)
        '''
        finally:
            # Stop machine
            mmanager.stop(vm.label)
            # Release the machine from lock
            mmanager.release(vm.label)
        '''
        # Launch reports generation
        Reporter(self.analysis.results_folder).run(Processor(self.analysis.results_folder).run())

        log.info("Task #%s: reports generation completed (path=%s)" % (self.task.id, self.analysis.results_folder))

    def run(self):
        """Run manager thread."""
        success = True

        db = Database()
        db.lock(self.task.id)

        try:
            self.launch_analysis()
        except CuckooMachineError as e:
            log.error("Please check virtual machine status: %s" % e)
            success = False
        except CuckooAnalysisError as e:
            log.error(e)
            success = False
        finally:
            db.complete(self.task.id, success)

class Scheduler:
    """Task scheduler."""

    def __init__(self):
        self.running = True
        self.cfg = Config()
        self.db = Database()

    def initialize(self):
        """Initialize machine manager."""
        global mmanager

        log.info("Using \"%s\" machine manager" % self.cfg.cuckoo.machine_manager)
        name = "modules.machinemanagers.%s" % self.cfg.cuckoo.machine_manager

        try:
            __import__(name, globals(), locals(), ["dummy"], -1)
        except ImportError as e:
            raise CuckooMachineError("Unable to import machine manager plugin: %s" % e)

        MachineManager()
        module = MachineManager.__subclasses__()[0]
        mmanager = module()
        mmanager_conf = os.path.join(CUCKOO_ROOT, "conf", "%s.conf" % self.cfg.cuckoo.machine_manager)

        if not os.path.exists(mmanager_conf):
            raise CuckooMachineError("The configuration file for machine manager \"%s\" does not exist at path: %s"
                                     % (self.cfg.cuckoo.machine_manager, mmanager_conf))

        mmanager.set_options(Config(mmanager_conf))
        mmanager.initialize(self.cfg.cuckoo.machine_manager)

        if len(mmanager.machines) == 0:
            raise CuckooMachineError("No machines available")
        else:
            log.info("Loaded %s machine/s" % len(mmanager.machines))

    def stop(self):
        """Stop scheduler."""
        self.running = False

        # Shutdown vm alive.
        # TODO: in future this code may be moved.
        if len(mmanager.running()) > 0:
            log.info("Shutting down guests")
            for machine in mmanager.running():
                try:
                    mmanager.stop(machine.label)
                except CuckooMachineError as e:
                    log.error("Unable to shutdown machine %s, please check manually. Error: %s" % (machine.label, e))

    def start(self):
        """Start scheduler."""
        self.initialize()

        log.info("Waiting for analysis tasks...")

        while self.running:
            time.sleep(1)

            if mmanager.availables() == 0:
                #log.debug("No machines available, try again")
                continue

            task = self.db.fetch()

            if not task:
                #log.debug("No pending tasks, try again")
                continue

            analysis = AnalysisManager(task)
            analysis.daemon = True
            analysis.start()

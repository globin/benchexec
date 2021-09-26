# This file is part of BenchExec, a framework for reliable benchmarking:
# https://github.com/sosy-lab/benchexec
#
# SPDX-FileCopyrightText: 2007-2020 Dirk Beyer <https://www.sosy-lab.org>
#
# SPDX-License-Identifier: Apache-2.0

import errno
import grp
import logging
import os
import pathlib
import shutil
import signal
import stat
import sys
import tempfile
import time

from benchexec import systeminfo
from benchexec import util
from benchexec.cgroups import Cgroups


# FIXME uid
CGROUP_FALLBACK_PATH = "user.slice/user-1000.slice/user@1000.service/app.slice/benchexec-cgroup.service/benchexec_root"
"""If we do not have write access to the current cgroup,
attempt to use this cgroup as fallback."""

CGROUP_NAME_PREFIX = "benchmark_"


_PERMISSION_HINT_GROUPS = """
You need to add your account to the following groups: {0}
Remember to logout and login again afterwards to make group changes effective."""

_PERMISSION_HINT_DEBIAN = """
The recommended way to fix this is to install the Debian package for BenchExec and add your account to the group "benchexec":
https://github.com/sosy-lab/benchexec/blob/master/doc/INSTALL.md#debianubuntu
Alternatively, you can install benchexec-cgroup.service manually:
https://github.com/sosy-lab/benchexec/blob/master/doc/INSTALL.md#setting-up-cgroups-on-machines-with-systemd"""

_PERMISSION_HINT_SYSTEMD = """
The recommended way to fix this is to add your account to a group named "benchexec" and install benchexec-cgroup.service:
https://github.com/sosy-lab/benchexec/blob/master/doc/INSTALL.md#setting-up-cgroups-on-machines-with-systemd"""

_PERMISSION_HINT_OTHER = """
Please configure your system in way to allow your user to use cgroups:
https://github.com/sosy-lab/benchexec/blob/master/doc/INSTALL.md#setting-up-cgroups-on-machines-without-systemd"""

_ERROR_MSG_PERMISSIONS = """
Required cgroups are not available because of missing permissions.{0}

As a temporary workaround, you can also run
"sudo chmod o+wt {1}"
Note that this will grant permissions to more users than typically desired and it will only last until the next reboot."""

_ERROR_MSG_OTHER = """
Required cgroups are not available.
If you are using BenchExec within a container, please make "/sys/fs/cgroup" available."""


def _find_cgroup_mount():
    """
    Return the mountpoint of the cgroupv2 unified hierarchy.
    @return Path mountpoint
    """
    try:
        with open("/proc/mounts", "rt") as mountsFile:
            for mount in mountsFile:
                mount = mount.split(" ")
                if mount[2] == "cgroup2":
                    return pathlib.Path(mount[1])
    except OSError:
        logging.exception("Cannot read /proc/mounts")


def _find_own_cgroups():
    """
    For all subsystems, return the information in which (sub-)cgroup this process is in.
    (Each process is in exactly cgroup in each hierarchy.)
    @return a generator of tuples (subsystem, cgroup)
    """
    try:
        with open("/proc/self/cgroup", "rt") as ownCgroupsFile:
            return _parse_proc_pid_cgroup(ownCgroupsFile)
    except OSError:
        logging.exception("Cannot read /proc/self/cgroup")


def _parse_proc_pid_cgroup(cgroup_file):
    """
    Parse a /proc/*/cgroup file into tuples of (subsystem,cgroup).
    @param content: An iterable over the lines of the file.
    @return: a generator of tuples
    """
    mountpoint = _find_cgroup_mount()
    own_cgroup = cgroup_file.readline().strip().split(":")
    path = mountpoint / own_cgroup[2]

    return path


def kill_all_tasks_in_cgroup(cgroup, ensure_empty=True):
    tasksFile = cgroup / "cgroup.procs"

    i = 0
    while True:
        i += 1
        # TODO We can probably remove this loop over signals and just send
        # SIGKILL. We added this loop when killing sub-processes was not reliable
        # and we did not know why, but now it is reliable.
        for sig in [signal.SIGKILL, signal.SIGINT, signal.SIGTERM]:
            with open(tasksFile, "rt") as tasks:
                task = None
                for task in tasks:
                    task = task.strip()
                    if i > 1:
                        logging.warning(
                            "Run has left-over process with pid %s "
                            "in cgroup %s, sending signal %s (try %s).",
                            task,
                            cgroup,
                            sig,
                            i,
                        )
                    util.kill_process(int(task), sig)

                if task is None or not ensure_empty:
                    return  # No process was hanging, exit
            # wait for the process to exit, this might take some time
            time.sleep(i * 0.5)


def remove_cgroup(cgroup):
    if not os.path.exists(cgroup):
        logging.warning("Cannot remove CGroup %s, because it does not exist.", cgroup)
        return
    assert os.path.getsize(cgroup / "cgroup.procs") == 0
    try:
        os.rmdir(cgroup)
    except OSError:
        # sometimes this fails because the cgroup is still busy, we try again once
        try:
            os.rmdir(cgroup)
        except OSError as e:
            logging.warning(
                "Failed to remove cgroup %s: error %s (%s)", cgroup, e.errno, e.strerror
            )


class CgroupsV2(Cgroups):
    def __init__(self, subsystems=None, cgroup_procinfo=None, fallback=True):
        self.version = 2

        self.IO = "io"
        self.CPU = "cpu"
        self.CPUSET = "cpuset"
        self.MEMORY = "memory"
        self.PID = "pids"
        self.FREEZE = "freeze"

        self.KNOWN_SUBSYSTEMS = {
            # cgroups for BenchExec
            self.IO,
            self.CPU,
            self.CPUSET,
            self.MEMORY,
            self.PID,
            # not really a subsystem anymore, but implicitly supported
            self.FREEZE,
        }

        super(CgroupsV2, self).__init__(subsystems, cgroup_procinfo, fallback)

        self.path = next(iter(self.subsystems.values()))

    def _supported_cgroup_subsystems(self, cgroup_procinfo=None, fallback=True):
        logging.debug(
            "Analyzing /proc/mounts and /proc/self/cgroup to determine cgroups."
        )
        if cgroup_procinfo is None:
            cgroup_path = _find_own_cgroups()
        else:
            cgroup_path = _parse_proc_pid_cgroup(cgroup_procinfo)

        if fallback:
            mount = _find_cgroup_mount()
            fallback_path = mount / CGROUP_FALLBACK_PATH
            cgroup_path = fallback_path

        with open(cgroup_path / "cgroup.subsystems") as subsystems_file:
            subsystems = subsystems_file.readline().strip().split()

        # always supported in v2
        subsystems.append(self.FREEZE)

        return {k: cgroup_path for k in subsystems}

    def create_fresh_child_cgroup(self, *subsystems):
        """
        Create child cgroups of the current cgroup for at least the given subsystems.
        @return: A Cgroup instance representing the new child cgroup(s).
        """
        assert set(subsystems).issubset(self.subsystems.keys())
        cgroup_path = pathlib.Path(
            tempfile.mkdtemp(prefix=CGROUP_NAME_PREFIX, dir=self.path)
        )

        # FIXME do something with subsystems, also subtree_control?
        return CgroupsV2({c: cgroup_path for c in self.subsystems})

    def add_task(self, pid):
        """
        Add a process to the cgroups represented by this instance.
        """
        with open(self.path / "cgroup.procs", "w") as tasksFile:
            tasksFile.write(str(pid))

    def get_all_tasks(self, subsystem):
        """
        Return a generator of all PIDs currently in this cgroup for the given subsystem.
        """
        with open(self.path / "cgroup.procs") as tasksFile:
            for line in tasksFile:
                yield int(line)

    def kill_all_tasks(self):
        """
        Kill all tasks in this cgroup and all its children cgroups forcefully.
        Additionally, the children cgroups will be deleted.
        """

        def kill_all_tasks_in_cgroup_recursively(cgroup, delete):
            for dirpath, dirs, _files in os.walk(cgroup, topdown=False):
                for subCgroup in dirs:
                    subCgroup = os.path.join(dirpath, subCgroup)
                    kill_all_tasks_in_cgroup(subCgroup, ensure_empty=delete)

                    if delete:
                        remove_cgroup(subCgroup)

            kill_all_tasks_in_cgroup(cgroup, ensure_empty=delete)

        # First, we go through all cgroups recursively while they are frozen and kill
        # all processes. This helps against fork bombs and prevents processes from
        # creating new subgroups while we are trying to kill everything.
        # All processes will stay until they are thawed (so we cannot check for cgroup
        # emptiness and we cannot delete subgroups).
        freezer_file = self.path / "cgroup.freeze"

        util.write_file("1", freezer_file)
        kill_all_tasks_in_cgroup_recursively(self.path, delete=False)
        util.write_file("0", freezer_file)

        # Second, we go through all cgroups again, kill what is left,
        # check for emptiness, and remove subgroups.
        kill_all_tasks_in_cgroup_recursively(self.path, delete=True)

    def has_value(self, subsystem, option):
        """
        Check whether the given value exists in the given subsystem.
        Does not make a difference whether the value is readable, writable, or both.
        Do not include the subsystem name in the option name.
        Only call this method if the given subsystem is available.
        """
        assert subsystem in self
        return os.path.isfile(self.path / f"{subsystem}.{option}")

    def get_value(self, subsystem, option):
        """
        Read the given value from the given subsystem.
        Do not include the subsystem name in the option name.
        Only call this method if the given subsystem is available.
        """
        assert subsystem in self, f"Subsystem {subsystem} is missing"
        return util.read_file(self.path / f"{subsystem}.{option}")

    def get_file_lines(self, subsystem, option):
        """
        Read the lines of the given file from the given subsystem.
        Do not include the subsystem name in the option name.
        Only call this method if the given subsystem is available.
        """
        assert subsystem in self
        with open(
            os.path.join(self.per_subsystem[subsystem], f"{subsystem}.{option}")
        ) as f:
            for line in f:
                yield line

    def get_key_value_pairs(self, subsystem, filename):
        """
        Read the lines of the given file from the given subsystem
        and split the lines into key-value pairs.
        Do not include the subsystem name in the option name.
        Only call this method if the given subsystem is available.
        """
        # FIXME v2 has basic cpu support even if not enabled
        # assert subsystem in self
        return util.read_key_value_pairs_from_file(
            self.path / f"{subsystem}.{filename}"
        )

    def set_value(self, subsystem, option, value):
        """
        Write the given value for the given subsystem.
        Do not include the subsystem name in the option name.
        Only call this method if the given subsystem is available.
        """
        assert subsystem in self
        util.write_file(
            str(value), self.subsystems[subsystem] / f"{subsystem}.{option}"
        )

    def remove(self):
        """
        Remove all cgroups this instance represents from the system.
        This instance is afterwards not usable anymore!
        """
        remove_cgroup(self.path)

        # FIXME why, we're not C?
        del self.subsystems

    def read_cputime(self):
        """
        Read the cputime usage of this cgroup. CPU cgroup needs to be available.
        @return cputime usage in seconds
        """
        cpu_stats = dict(self.get_key_value_pairs(self.CPU, "stat"))

        return float(cpu_stats["usage_usec"]) / 1_000_000

    def read_allowed_memory_banks(self):
        """Get the list of all memory banks allowed by this cgroup."""
        return util.parse_int_list(self.get_value(CPUSET, "mems"))

    def read_max_mem_usage(self):
        logging.debug("Memory-usage not supported in cgroups v2")

        return None

    def read_usage_per_cpu(self):
        logging.debug("Usage per CPU not supported in cgroups v2")

        return {}

    def read_available_cpus(self):
        return util.parse_int_list(self.get_value(self.CPUSET, "cpus.effective"))

    def read_available_mems(self):
        return util.parse_int_list(self.get_value(self.CPUSET, "mems.effective"))

"""
Start an IPython Qt console or notebook connected to the python session
running in Excel.

This requires sys.executable to be set, and so it's recommended
that the following is added to the pyxll.cfg file:

[PYTHON]
executable = <path to your python installation>/pythonw.exe
"""
from .magic import ExcelMagics
from ipykernel.kernelapp import IPKernelApp
from ipykernel.embed import embed_kernel
from zmq.eventloop import ioloop
from pyxll import schedule_call
import subprocess
import threading
import logging
import atexit
import sys
import os
import re

_log = logging.getLogger(__name__)
_all_jupyter_processes = []

try:
    import win32api
except ImportError:
    win32api = None

if getattr(sys, "_ipython_kernel_running", None) is None:
    sys._ipython_kernel_running = False

if getattr(sys, "_ipython_app", None) is None:
    sys._ipython_app = False


def _which(program):
    """find an exe's full path by looking at the PATH environment variable"""
    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            path = path.strip('"')
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file

    return None


class PushStdout:
    """Context manage to temporarily replace stdout/stderr."""

    def __init__(self, stdout, stderr):
        self.__stdout = stdout
        self.__stderr = stderr

    def __enter__(self):
        self.__orig_stdout = sys.stdout
        self.__orig_stderr = sys.stderr
        sys.stdout = self.__stdout
        sys.stderr = self.__stderr

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout = self.__orig_stdout
        sys.stderr = self.__orig_stderr


def start_kernel():
    """starts the ipython kernel and returns the ipython app"""
    if sys._ipython_app and sys._ipython_kernel_running:
        return sys._ipython_app

    # The stdout/stderrs used by IPython. These get set after the kernel has started.
    ipy_stdout = sys.stdout
    ipy_stderr = sys.stderr

    # patch IPKernelApp.start so that it doesn't block
    def _IPKernelApp_start(self):
        nonlocal ipy_stdout, ipy_stderr

        if self.poller is not None:
            self.poller.start()
        self.kernel.start()

        # set up a timer to periodically poll the zmq ioloop
        self.loop = ioloop.IOLoop.current()

        def poll_ioloop():
            try:
                # Use the IPython stdout/stderr while running the kernel
                with PushStdout(ipy_stdout, ipy_stderr):
                    # If the kernel has been closed then run the event loop until it gets to the
                    # stop event added by IPKernelApp.shutdown_request
                    if self.kernel.shell.exit_now:
                        _log.debug("IPython kernel stopping (%s)" % self.connection_file)
                        self.loop.start()
                        sys._ipython_kernel_running = False
                        return

                    # otherwise call the event loop but stop immediately if there are no pending events
                    self.loop.add_timeout(0, lambda: self.loop.add_callback(self.loop.stop))
                    self.loop.start()
            except:
                _log.error("Error polling Jupyter loop", exc_info=True)

            schedule_call(poll_ioloop, delay=0.1)

        sys._ipython_kernel_running = True
        schedule_call(poll_ioloop, delay=0.1)

    IPKernelApp.start = _IPKernelApp_start

    # IPython expects sys.__stdout__ to be set, and keep the original values to
    # be used after IPython has set its own.
    sys.__stdout__ = sys_stdout = sys.stdout
    sys.__stderr__ = sys_stderr = sys.stderr

    # call the API embed function, which will use the monkey-patched method above
    embed_kernel(local_ns={})

    ipy = IPKernelApp.instance()

    # register the magic functions
    ipy.shell.register_magics(ExcelMagics)

    # Keep a reference to the kernel even if this module is reloaded
    sys._ipython_app = ipy

    # Restore sys stdout/stderr and keep track of the IPython versions
    ipy_stdout = sys.stdout
    ipy_stderr = sys.stderr
    sys.stdout = sys_stdout
    sys.stderr = sys_stderr

    # patch user_global_ns so that it always references the user_ns dict
    setattr(ipy.shell.__class__, 'user_global_ns', property(lambda self: self.user_ns))

    # patch ipapp so anything else trying to get a terminal app (e.g. ipdb) gets our IPKernalApp.
    from IPython.terminal.ipapp import TerminalIPythonApp
    TerminalIPythonApp.instance = lambda: ipy

    # Use the inline matplotlib backend
    mpl = ipy.shell.find_magic("matplotlib")
    if mpl:
        try:
            mpl("inline")
        except ImportError:
            pass

    return ipy


def launch_jupyter(connection_file, cwd=None):
    """Launch a Jupyter notebook server as a child process.

    :param connection_file: File for kernels to use to connect to an existing kernel.
    :param cwd: Current working directory to start the notebook in.
    :return: (Popen2 instance, URL string)
    """

    # Find jupyter-notebook.exe in the Scripts path local to python.exe
    jupyter_notebook = None
    if sys.executable and os.path.basename(sys.executable) in ("python.exe", "pythonw.exe"):
        for path in (os.path.dirname(sys.executable), os.path.join(os.path.dirname(sys.executable), "Scripts")):
            jupyter_notebook = os.path.join(path, "jupyter-notebook.exe")
            if os.path.exists(jupyter_notebook):
                break

    # If it wasn't found look for it on the system path
    if jupyter_notebook is None or not os.path.exists(jupyter_notebook):
        jupyter_notebook = _which("jupyter-notebook.exe")

    if jupyter_notebook is None or not os.path.exists(jupyter_notebook):
        raise Exception("jupyter-notebook.exe not found")

    # Use the current python path when launching
    env = dict(os.environ)
    env["PYTHONPATH"] = ";".join(sys.path)

    # Set PYXLL_IPYTHON_CONNECTION_FILE so the manager knows what to connect to
    env["PYXLL_IPYTHON_CONNECTION_FILE"] = connection_file

    # run jupyter in it's own process
    cmd = [
        jupyter_notebook,
        "--NotebookApp.kernel_manager_class=pyxll_jupyter.extipy.ExternalIPythonKernelManager",
        "--no-browser",
        "-y"
    ]
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if proc.poll() is not None:
        raise Exception("Command '%s' failed to start" % " ".join(cmd))

    # Add it to the list of processes to be killed when Excel exits
    _all_jupyter_processes.append(proc)

    # Find the URL to connect to from the output
    url = None
    i = 0
    while url is None and i < 25 and proc.poll() is None:
        line = proc.stdout.readline().decode().strip()
        if line.startswith("DEBUG"):
            _log.debug(line)
            continue
        i += 1
        _log.info(line)
        match = re.match(".*(https?://[\w+\.]+(:\d+)?/\?token=\w+)", line)
        if match:
            url = match.group(1)
            break
    else:
        raise RuntimeError("Failed to find URL in output of jupyter-notebook command.")

    # Monitor the output in a couple of background threads
    def thread_func():
        while proc.poll() is None:
            _log.info(proc.stdout.readline().decode().rstrip())

    thread = threading.Thread(target=thread_func)
    thread.daemon = True
    thread.start()

    return proc, url


@atexit.register
def _kill_jupyter_processes():
    """Ensure all Jupyter processes are killed."""
    global _all_jupyter_processes
    while _all_jupyter_processes:
        proc = _all_jupyter_processes[0]
        if proc.poll() is not None:
            _all_jupyter_processes = _all_jupyter_processes[1:]
            continue
        _log.info("Killing Jupyter process %s" % proc.pid)
        si = subprocess.STARTUPINFO(wShowWindow=subprocess.SW_HIDE)
        subprocess.check_call(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                              startupinfo=si,
                              shell=True)

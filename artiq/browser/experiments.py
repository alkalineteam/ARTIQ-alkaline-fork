import asyncio
import logging
import os
from functools import partial
from collections import OrderedDict

from PyQt6 import QtCore, QtGui, QtWidgets
import h5py

from artiq import __artiq_dir__ as artiq_dir
from artiq.gui.tools import (LayoutWidget, log_level_to_name, get_open_file_name)
from artiq.gui.entries import procdesc_to_entry, EntryTreeWidget
from artiq.master.worker import Worker, log_worker_exception
from artiq import compat

logger = logging.getLogger(__name__)


class _ArgumentEditor(EntryTreeWidget):
    def __init__(self, dock):
        EntryTreeWidget.__init__(self)
        self._dock = dock

        if not self._dock.arguments:
            self.insertTopLevelItem(0, QtWidgets.QTreeWidgetItem(["No arguments"]))

        for name, argument in self._dock.arguments.items():
            self.set_argument(name, argument)

        self.quickStyleClicked.connect(self._dock._run_clicked)

        recompute_arguments = QtWidgets.QPushButton("Recompute all arguments")
        recompute_arguments.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_BrowserReload))
        recompute_arguments.clicked.connect(self._recompute_arguments_clicked)

        load = QtWidgets.QPushButton("Set arguments from HDF5")
        load.setToolTip("Set arguments from currently selected HDF5 file")
        load.setIcon(QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_DialogApplyButton))
        load.clicked.connect(self._load_clicked)

        buttons = LayoutWidget()
        buttons.addWidget(recompute_arguments, 1, 1)
        buttons.addWidget(load, 1, 2)
        for i, s in enumerate((1, 0, 0, 1)):
            buttons.layout.setColumnStretch(i, s)
        self.setItemWidget(self.bottom_item, 1, buttons)

    def _load_clicked(self):
        asyncio.ensure_future(self._dock.load_hdf5_task())

    def _recompute_arguments_clicked(self):
        asyncio.ensure_future(self._dock._recompute_arguments())

    def reset_entry(self, key):
        asyncio.ensure_future(self._recompute_argument(key))

    async def _recompute_argument(self, name):
        try:
            arginfo = await self._dock.compute_arginfo()
        except:
            logger.error("Could not recompute argument '%s' of '%s'",
                         name, self._dock.expurl, exc_info=True)
            return
        argument = self._dock.arguments[name]

        procdesc = arginfo[name][0]
        state = procdesc_to_entry(procdesc).default_state(procdesc)
        argument["desc"] = procdesc
        argument["state"] = state
        self.update_argument(name, argument)


log_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class _ExperimentDock(QtWidgets.QMdiSubWindow):
    sigClosed = QtCore.pyqtSignal()

    def __init__(self, area, expurl, arguments):
        QtWidgets.QMdiSubWindow.__init__(self)
        qfm = QtGui.QFontMetrics(self.font())
        self.resize(100*qfm.averageCharWidth(), 30*qfm.lineSpacing())
        self.setWindowTitle(expurl)
        self.setWindowIcon(QtWidgets.QApplication.style().standardIcon(
            QtWidgets.QStyle.StandardPixmap.SP_FileDialogContentsView))
        self.setAcceptDrops(True)

        self.layout = QtWidgets.QGridLayout()
        top_widget = QtWidgets.QWidget()
        top_widget.setLayout(self.layout)
        self.setWidget(top_widget)
        self.layout.setSpacing(5)
        self.layout.setContentsMargins(5, 5, 5, 5)

        self._area = area
        self._run_task = None
        self.expurl = expurl
        self.arguments = arguments
        self.options = {"log_level": logging.WARNING}

        self.argeditor = _ArgumentEditor(self)
        self.layout.addWidget(self.argeditor, 0, 0, 1, 5)
        self.layout.setRowStretch(0, 1)

        log_level = QtWidgets.QComboBox()
        log_level.addItems(log_levels)
        log_level.setCurrentIndex(1)
        log_level.setToolTip("Minimum level for log entry production")
        log_level_label = QtWidgets.QLabel("Logging level:")
        log_level_label.setToolTip("Minimum level for log message production")
        self.layout.addWidget(log_level_label, 3, 0)
        self.layout.addWidget(log_level, 3, 1)

        log_level.setCurrentIndex(log_levels.index(
            log_level_to_name(self.options["log_level"])))

        def update_log_level(index):
            self.options["log_level"] = getattr(logging,
                                                log_level.currentText())
        log_level.currentIndexChanged.connect(update_log_level)
        self.log_level = log_level

        run = QtWidgets.QPushButton("Analyze")
        run.setIcon(QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_DialogOkButton))
        run.setToolTip("Run analysis stage (Ctrl+Return)")
        run.setShortcut("CTRL+RETURN")
        run.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                          QtWidgets.QSizePolicy.Policy.Expanding)
        self.layout.addWidget(run, 2, 4)
        run.clicked.connect(self._run_clicked)
        self._run = run

        terminate = QtWidgets.QPushButton("Terminate")
        terminate.setIcon(QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_DialogCancelButton))
        terminate.setToolTip("Terminate analysis (Ctrl+Backspace)")
        terminate.setShortcut("CTRL+BACKSPACE")
        terminate.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                QtWidgets.QSizePolicy.Policy.Expanding)
        self.layout.addWidget(terminate, 3, 4)
        terminate.clicked.connect(self._terminate_clicked)
        terminate.setEnabled(False)
        self._terminate = terminate

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasFormat("text/uri-list"):
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for uri in ev.mimeData().urls():
            if uri.scheme() == "file":
                filename = QtCore.QDir.toNativeSeparators(uri.toLocalFile())
                logger.debug("Loading HDF5 arguments from %s", filename)
                asyncio.ensure_future(self.load_hdf5_task(filename))
                break

    async def compute_arginfo(self):
        return await self._area.compute_arginfo(self.expurl)

    async def _recompute_arguments(self, overrides={}):
        try:
            arginfo = await self.compute_arginfo()
        except:
            logger.error("Could not recompute arguments of '%s'",
                         self.expurl, exc_info=True)
            return
        for k, v in overrides.items():
            # Some values (e.g. scans) may have multiple defaults in a list
            if isinstance(arginfo[k][0].get("default"), list):
                arginfo[k][0]["default"].insert(0, v)
            else:
                arginfo[k][0]["default"] = v
        self.arguments = self._area.initialize_submission_arguments(arginfo)

        state = self.argeditor.save_state()
        self.argeditor.deleteLater()
        self.argeditor = _ArgumentEditor(self)
        self.layout.addWidget(self.argeditor, 0, 0, 1, 5)
        self.argeditor.restore_state(state)

    async def load_hdf5_task(self, filename=None):
        if filename is None:
            if self._area.dataset is None:
                return
            filename = self._area.dataset

        try:
            with h5py.File(filename, "r") as f:
                expid = f["expid"][()]
            expid = compat.pyon_decode(expid)
            arguments = expid["arguments"]
        except:
            logger.error("Could not retrieve expid from HDF5 file",
                         exc_info=True)
            return

        try:
            self.log_level.setCurrentIndex(log_levels.index(
                log_level_to_name(expid["log_level"])))
        except:
            logger.error("Could not set submission options from HDF5 expid",
                         exc_info=True)
            return

        await self._recompute_arguments(arguments)

    def _run_clicked(self):
        class_name, file = self.expurl.split("@", maxsplit=1)
        expid = {
            "repo_rev": "N/A",
            "file": file,
            "class_name": class_name,
            "log_level": self.options["log_level"],
            "arguments": {
                name: procdesc_to_entry(argument["desc"]).state_to_value(
                    argument["state"])
                for name, argument in self.arguments.items()},
        }
        self._run_task = asyncio.ensure_future(self._get_run_task(expid))
        self._run.setEnabled(False)
        self._terminate.setEnabled(True)

        def done(fut):
            logger.debug("Analysis done")
            self._run_task = None
            self._run.setEnabled(True)
            self._terminate.setEnabled(False)
        self._run_task.add_done_callback(done)

    async def _get_run_task(self, expid):
        logger.info("Running '%s'...", self.expurl)
        worker = Worker(self._area.worker_handlers)
        try:
            await worker.build(rid=None, pipeline_name="browser",
                               wd=os.path.abspath("."),
                               expid=expid, priority=0)
            await worker.analyze()
        except:
            logger.error("Failed to run '%s'", self.expurl)
            log_worker_exception()
        else:
            logger.info("Finished running '%s'", self.expurl)
        finally:
            await worker.close()

    def _terminate_clicked(self):
        try:
            self._run_task.cancel()
        except:
            logger.error("Unexpected failure terminating '%s'",
                         self.expurl, exc_info=True)

    def closeEvent(self, event):
        self.sigClosed.emit()
        QtWidgets.QMdiSubWindow.closeEvent(self, event)

    def save_state(self):
        return {
            "argeditor": self.argeditor.save_state(),
            "geometry": bytes(self.saveGeometry()),
            "options": self.options,
        }

    def restore_state(self, state):
        self.argeditor.restore_state(state["argeditor"])
        self.restoreGeometry(QtCore.QByteArray(state["geometry"]))
        self.options = state["options"]


class LocalDatasetDB:
    def __init__(self, dataset_sub):
        self.dataset_sub = dataset_sub
        dataset_sub.add_setmodel_callback(self.init)

    def init(self, data):
        self._data = data

    def get(self, key):
        return self._data.backing_store[key][1]

    def update(self, mod):
        self.dataset_sub.update(mod)


class ExperimentsArea(QtWidgets.QMdiArea):
    def __init__(self, root, dataset_sub):
        QtWidgets.QMdiArea.__init__(self)
        self.pixmap = QtGui.QPixmap(os.path.join(
            artiq_dir, "gui", "logo_ver.svg"))
        self.current_dir = root
        self.dataset = None

        self.open_experiments = []

        self._ddb = LocalDatasetDB(dataset_sub)

        self.worker_handlers = {
            "get_device_db": lambda: {},
            "get_device": lambda key, resolve_alias=False: {"type": "dummy"},
            "get_dataset": self._ddb.get,
            "update_dataset": self._ddb.update,
        }

    def dataset_changed(self, path):
        self.dataset = path

    def dataset_activated(self, path):
        sub = self.currentSubWindow()
        if sub is None:
            return
        asyncio.ensure_future(sub.load_hdf5_task(path))

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            self.select_experiment()

    def paintEvent(self, event):
        QtWidgets.QMdiArea.paintEvent(self, event)
        painter = QtGui.QPainter(self.viewport())
        x = (self.width() - self.pixmap.width())//2
        y = (self.height() - self.pixmap.height())//2
        painter.setOpacity(0.5)
        painter.drawPixmap(x, y, self.pixmap)

    def save_state(self):
        return {"experiments": [{
            "expurl": dock.expurl,
            "arguments": dock.arguments,
            "dock": dock.save_state(),
        } for dock in self.open_experiments]}

    def restore_state(self, state):
        if self.open_experiments:
            raise NotImplementedError
        for ex_state in state["experiments"]:
            dock = self.open_experiment(ex_state["expurl"],
                                        ex_state["arguments"])
            dock.restore_state(ex_state["dock"])

    def select_experiment(self):
        asyncio.ensure_future(self._select_experiment_task())

    async def _select_experiment_task(self):
        try:
            file = await get_open_file_name(
                self, "Open experiment", self.current_dir,
                "Experiments (*.py);;All files (*.*)")
        except asyncio.CancelledError:
            return
        self.current_dir = os.path.dirname(file)
        logger.debug("Opening experiment %s", file)
        try:
            description = await self.examine(file)
        except:
            logger.error("Could not examine experiment '%s'",
                         file, exc_info=True)
            return
        for class_name, class_desc in description.items():
            expurl = "{}@{}".format(class_name, file)
            arguments = self.initialize_submission_arguments(
                class_desc["arginfo"])
            self.open_experiment(expurl, arguments)

    def initialize_submission_arguments(self, arginfo):
        arguments = OrderedDict()
        for name, (procdesc, group, tooltip) in arginfo.items():
            if procdesc["ty"] == "EnumerationValue" and procdesc["quickstyle"]:
                procdesc["quickstyle"] = False
            state = procdesc_to_entry(procdesc).default_state(procdesc)
            arguments[name] = {
                "desc": procdesc,
                "group": group,
                "tooltip": tooltip,
                "state": state  # mutated by entries
            }
        return arguments

    async def examine(self, file):
        worker = Worker(self.worker_handlers)
        try:
            return await worker.examine("examine", file)
        finally:
            await worker.close()

    async def compute_arginfo(self, expurl):
        class_name, file = expurl.split("@", maxsplit=1)
        try:
            desc = await self.examine(file)
        except:
            logger.error("Could not examine experiment '%s'",
                         file, exc_info=True)
            return
        return desc[class_name]["arginfo"]

    def open_experiment(self, expurl, arguments):
        try:
            dock = _ExperimentDock(self, expurl, arguments)
        except:
            logger.warning("Failed to create experiment dock for %s, "
                           "retrying with arguments reset", expurl,
                           exc_info=True)
            dock = _ExperimentDock(self, expurl, {})
            asyncio.ensure_future(dock._recompute_arguments())
        dock.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.addSubWindow(dock)
        dock.show()
        dock.sigClosed.connect(partial(self.on_dock_closed, dock))
        self.open_experiments.append(dock)
        return dock

    def set_argument_value(self, expurl, name, value):
        logger.warning("Unable to set argument '%s', dropping change. "
                       "'set_argument_value' not supported in browser.", name)

    def on_dock_closed(self, dock):
        self.open_experiments.remove(dock)

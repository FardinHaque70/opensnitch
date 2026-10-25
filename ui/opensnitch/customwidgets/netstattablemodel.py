from PyQt5.QtCore import Qt
from PyQt5.QtGui import QStandardItemModel
from opensnitch.customwidgets.generictableview import GenericTableModel
from opensnitch.customwidgets.applicationidentity import IdentityPresentation
from opensnitch.utils import sockets

class NetstatTableModel(GenericTableModel):

    def __init__(self, tableName, headerLabels):
        super().__init__(tableName, headerLabels)

        self.COL_STATE =2
        self.COL_PROTO = 7
        self.COL_FAMILY = 10

    def data(self, index, role=Qt.DisplayRole):
        """Paint rows with the data stored in self.items"""
        if role == Qt.DisplayRole or role == Qt.EditRole:
            items_count = len(self.items)
            if index.isValid() and items_count > 0 and index.row() < items_count:
                try:
                    # FIXME: protocol UDP + state CLOSE == state LISTEN
                    if index.column() == self.COL_STATE:
                        return sockets.State[self.items[index.row()][index.column()]]
                    elif index.column() == self.COL_PROTO:
                        return sockets.Proto[self.items[index.row()][index.column()]]
                    elif index.column() == self.COL_FAMILY:
                        return sockets.Family[self.items[index.row()][index.column()]]
                    return self.items[index.row()][index.column()]
                except Exception as e:
                    print("[socketsmodel] exception:", e, index.row(), index.column())
        return QStandardItemModel.data(self, index, role)


class IdentityNetstatTableModel(IdentityPresentation, NetstatTableModel):
    def __init__(self, table_name, headers, resolver, source_node, view):
        super().__init__(table_name, headers)
        self.snapshot_node = None
        self._source_node = source_node
        self.configure_identity(headers, resolver, self.process_identity, view)

    def process_identity(self, index, row):
        if index.column() == 1 and len(row) == len(self._identity_headers):
            return self._identity_resolver.resolve(self.snapshot_node, row[1])
        return None

    def fillVisibleRows(self, query, upper_bound, force=False):
        # The picker can change while a previous snapshot remains visible.
        # Bind provenance when reading rows, never from the current picker.
        self.snapshot_node = self._source_node()
        super().fillVisibleRows(query, upper_bound, force)

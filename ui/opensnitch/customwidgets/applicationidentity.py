"""Application presentation roles, independent of table identifiers."""

from PyQt5 import QtCore, QtGui, QtWidgets

from opensnitch.customwidgets.generictableview import GenericTableModel


APPLICATION_IDENTITY_ROLE = QtCore.Qt.UserRole + 101


class IdentityPresentation:
    """Presentation roles shared by generic and specialized table models."""

    def configure_identity(self, headers, resolver, identity_for_cell, view):
        self._identity_resolver = resolver
        self._identity_for_cell = identity_for_cell
        self._identity_view = view
        self._identity_headers = tuple(headers)
        resolver.changed.connect(self.refresh_identities)

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if (orientation == QtCore.Qt.Horizontal and role == QtCore.Qt.DisplayRole
                and self.columnCount() == len(self._identity_headers)
                and 0 <= section < len(self._identity_headers)):
            return self._identity_headers[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if role in (APPLICATION_IDENTITY_ROLE, QtCore.Qt.DecorationRole,
                    QtCore.Qt.ToolTipRole, QtCore.Qt.AccessibleTextRole):
            if index.isValid() and 0 <= index.row() < len(self.items):
                identity = self._identity_for_cell(index, self.items[index.row()])
                if identity is not None:
                    if role == APPLICATION_IDENTITY_ROLE:
                        return identity
                    if role == QtCore.Qt.DecorationRole:
                        return self._identity_resolver.icon_for(identity, self._identity_view)
                    return identity.tooltip
        # Preserve DisplayRole/EditRole: existing actions use these as identifiers.
        return super().data(index, role)

    def refresh_identities(self):
        if self.items and self.columnCount():
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, self.columnCount() - 1))


class IdentityTableModel(IdentityPresentation, GenericTableModel):
    def __init__(self, table_name, headers, resolver, identity_for_cell, view):
        super().__init__(table_name, headers)
        self.configure_identity(headers, resolver, identity_for_cell, view)


def draw_application_identity(painter, option, index, foreground=None, background=None, alignment=None):
    identity = index.data(APPLICATION_IDENTITY_ROLE)
    if identity is None:
        return False
    styled = QtWidgets.QStyleOptionViewItem(option)
    QtWidgets.QStyledItemDelegate().initStyleOption(styled, index)
    styled.text = identity.display_name
    if foreground:
        styled.palette.setColor(QtGui.QPalette.Text, foreground)
    if background:
        styled.backgroundBrush = QtGui.QBrush(background)
    if alignment is not None:
        styled.displayAlignment = alignment
    style = option.widget.style() if option.widget is not None else QtWidgets.QApplication.style()
    style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, styled, painter, option.widget)
    return True


class ApplicationIdentityDelegate(QtWidgets.QStyledItemDelegate):
    def paint(self, painter, option, index):
        if not draw_application_identity(painter, option, index):
            super().paint(painter, option, index)

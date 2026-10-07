"""Small prepared-mail references; only helpers parse configuration or MIME."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
import os

from app.preparation_resources import ResourceBudget, SpoolReservation


@dataclass(frozen=True)
class PreparedMail:
    reservation: SpoolReservation
    artifacts: dict[str, str]

    def open_descriptors(self):
        """Return retained read-only files and the existing reservation guard."""
        stack = ExitStack()
        try:
            self.reservation.validate()
            budget = ResourceBudget(self.reservation.directory,
                                    self.reservation.directory_fd)
            stack.callback(budget.close)
            files = []
            for key in ('config', 'envelope', 'mime', 'mime_utf8'):
                name = self.artifacts[key]
                _, size = budget.verify(name)
                source = stack.enter_context(budget.open(name, 'rb'))
                if os.fstat(source.fileno()).st_size != size:
                    raise ValueError('prepared mail file changed')
                files.append(source.fileno())
            return stack, tuple(files), self.reservation.guard
        except BaseException:
            stack.close()
            raise

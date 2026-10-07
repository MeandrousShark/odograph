"""Verified read-only ntfy artifacts retained under their existing grant."""
from contextlib import ExitStack
from dataclasses import dataclass
import os

from app.preparation_resources import ResourceBudget, SpoolReservation


@dataclass(frozen=True)
class PreparedNtfy:
    reservation: SpoolReservation
    artifacts: dict[str, str]

    def open_descriptors(self):
        stack = ExitStack()
        try:
            self.reservation.validate()
            budget = ResourceBudget(self.reservation.directory, self.reservation.directory_fd)
            stack.callback(budget.close)
            descriptors = []
            for key in ('config', 'body', 'cookies'):
                name = self.artifacts[key]
                _, size = budget.verify(name)
                source = stack.enter_context(budget.open(name, 'rb'))
                if os.fstat(source.fileno()).st_size != size:
                    raise ValueError('prepared ntfy file changed')
                descriptors.append(source.fileno())
            return stack, tuple(descriptors), self.reservation.guard
        except BaseException:
            stack.close()
            raise

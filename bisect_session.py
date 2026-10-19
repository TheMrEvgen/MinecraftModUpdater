"""
Поиск проблемного мода делением пополам (без Qt).

На каждом шаге выключается половина подозреваемых — вместе с модами, которым они нужны
(иначе игра упадёт из-за недостающей зависимости, и результат будет ложным). Пользователь
запускает игру и отвечает, осталась ли проблема. Моды идентифицируются по имени файла без *.disabled.
"""
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Set


def dependency_closure(names: Iterable[str], requires: Dict[str, Set[str]]) -> Set[str]:
    """names + все моды, которые (через цепочку) требуют что-то из names."""
    result = set(names)
    changed = True
    while changed:
        changed = False
        for mod, deps in requires.items():
            if mod not in result and deps & result:
                result.add(mod)
                changed = True
    return result


@dataclass
class BisectSession:
    suspects: List[str]                     # Где-то среди них виновник
    original_disabled: List[str]            # Что было выключено до начала — восстановить в конце
    requires: Dict[str, List[str]]          # мод -> обязательные зависимости (имена файлов)
    step: int = 0
    disabled_now: List[str] = field(default_factory=list)  # Что выключено на текущем шаге

    @classmethod
    def start(cls, enabled: List[str], originally_disabled: List[str], requires: Dict[str, Set[str]]) -> "BisectSession":
        session = cls(sorted(enabled), sorted(originally_disabled),
                      {k: sorted(v) for k, v in requires.items()})
        session.disabled_now = sorted(session.plan())
        return session

    @property
    def done(self) -> bool:
        return len(self.suspects) <= 1

    @property
    def culprit(self) -> Optional[str]:
        return self.suspects[0] if len(self.suspects) == 1 else None

    def _requires(self) -> Dict[str, Set[str]]:
        return {k: set(v) for k, v in self.requires.items()}

    def plan(self) -> Set[str]:
        """Что выключить на этом шаге: первую половину подозреваемых и зависящие от неё моды."""
        requires = self._requires()
        middle = max(1, len(self.suspects) // 2)
        suspects = set(self.suspects)
        candidates = [self.suspects[:middle], self.suspects[middle:]]
        # Если половина тянет за собой всех подозреваемых (общая библиотека), пробуем другую половину,
        # а затем отдельные моды с самым маленьким «хвостом» зависящих
        candidates += sorted(([s] for s in self.suspects), key=lambda one: len(dependency_closure(one, requires)))
        for half in candidates:
            closure = dependency_closure(half, requires)
            if half and not suspects <= closure:
                return closure
        return dependency_closure(self.suspects[:1], requires)

    def answer(self, problem_persists: bool):
        """Проблема осталась — виновник среди включённых; пропала — среди выключенных."""
        disabled = set(self.disabled_now)
        if problem_persists:
            remaining = [s for s in self.suspects if s not in disabled]
        else:
            remaining = [s for s in self.suspects if s in disabled]
        # Если ответ противоречив (например, проблема не в модах), не теряем подозреваемых
        self.suspects = remaining or self.suspects[:1]
        self.step += 1
        self.disabled_now = [] if self.done else sorted(self.plan())

    def steps_left(self) -> int:
        n, steps = len(self.suspects), 0
        while n > 1:
            n = (n + 1) // 2
            steps += 1
        return steps

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "BisectSession":
        return cls(**{k: data[k] for k in ("suspects", "original_disabled", "requires", "step", "disabled_now")})

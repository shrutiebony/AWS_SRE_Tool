"""Record what a repair taught the agent. Lessons never authorize the next repair."""

from __future__ import annotations

from sre_agent.models import Lesson
from sre_agent.registry.store import Registry


class LessonStore:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def record(self, lesson: Lesson) -> None:
        self.registry.add_lesson(lesson)

    def recall(self, symptom_key: str) -> list[Lesson]:
        return self.registry.lessons_for(symptom_key)

    def all(self) -> list[Lesson]:
        return self.registry.backend.load_all("lessons")

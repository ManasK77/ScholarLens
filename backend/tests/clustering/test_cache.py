from unittest.mock import Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clustering.nli import ContradictionDetector
from app.core.pipeline import pipeline_run
from app.ingestion.corpus import Base


def test_nli_cache_preserves_direction_and_model_version():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    predict = Mock(side_effect=lambda pairs: [{"contradiction": 0.7} for _ in pairs])
    detector = ContradictionDetector(predict, 0.5, cache_version="model-v1")
    with Session(engine) as session:
        with pipeline_run(session, {}):
            assert detector._score([("left", "right")]) == [0.7]
        with pipeline_run(session, {}):
            assert detector._score([("left", "right")]) == [0.7]
            assert detector._score([("right", "left")]) == [0.7]
        assert predict.call_count == 2
        changed = ContradictionDetector(predict, 0.5, cache_version="model-v2")
        with pipeline_run(session, {}):
            changed._score([("left", "right")])
        assert predict.call_count == 3

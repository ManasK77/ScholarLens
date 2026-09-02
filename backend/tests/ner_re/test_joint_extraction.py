from app.ner_re.extractor import normalize_joint_result


def test_normalize_joint_result_preserves_typed_entities_and_relations():
    raw = {
        "entities": {
            "model": [{"text": "BERT", "confidence": 0.94}],
            "dataset": [{"text": "SQuAD", "confidence": 0.91}],
        },
        "relation_extraction": {
            "trained_on": [
                {
                    "head": {"text": "BERT", "confidence": 0.94},
                    "tail": {"text": "SQuAD", "confidence": 0.91},
                }
            ]
        },
    }

    result = normalize_joint_result(raw)

    assert {(item.text, item.entity_type) for item in result.entities} == {
        ("BERT", "model"),
        ("SQuAD", "dataset"),
    }
    assert result.relations[0].relation_type == "trained_on"
    assert result.relations[0].head == "BERT"
    assert result.relations[0].tail == "SQuAD"


def test_normalize_joint_result_resolves_real_joint_ie_entity_ids():
    raw = {
        "entities": [
            {"id": "e1", "type": "model", "text": "BERT", "confidence": 0.9},
            {"id": "e2", "type": "dataset", "text": "SQuAD", "confidence": 0.8},
        ],
        "relations": [
            {
                "type": "trained_on",
                "head": "e1",
                "tail": "e2",
                "confidence": 0.7,
            }
        ],
    }

    result = normalize_joint_result(raw)

    assert result.relations[0].head == "BERT"
    assert result.relations[0].tail == "SQuAD"

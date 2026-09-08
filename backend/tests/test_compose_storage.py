from pathlib import Path


def test_backend_pdf_directory_uses_a_persistent_named_volume():
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text()

    assert "pdf_data:/data/pdfs" in compose
    assert "pdf_data:" in compose.split("volumes:", 1)[1]

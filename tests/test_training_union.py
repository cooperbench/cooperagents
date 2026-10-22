"""Keep the frozen training selection equal to its three source subsets."""

import hashlib
import json
import re
from pathlib import Path

from cooperagents.eval.dataset import load_subset


def test_training_union(tmp_path):
    root = Path(__file__).resolve().parents[1]
    directory = root / "datasets/cb-mixture-36"
    manifest = json.loads((directory / "manifest.json").read_text())
    expected = set()
    for source in manifest["sources"].values():
        path = root / source["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source["sha256"]
        match = re.search(r'PAIRS="(.*?)"', path.read_text(), re.S)
        assert match is not None
        specs = match.group(1).replace("\\\n", " ").split()
        assert len(specs) == source["pairs"]
        for spec in specs:
            repo, task, features = spec.split(":")
            expected.add((repo, int(task), tuple(sorted(map(int, features.split(","))))))

    subsets = tmp_path / "dataset/subsets"
    subsets.mkdir(parents=True)
    (subsets / "union.json").write_bytes((directory / "manifest.json").read_bytes())
    items = load_subset("union", cooperbench_dir=tmp_path)
    actual = [(item.repo, item.task_id, tuple(item.features)) for item in items]
    assert actual == sorted(expected)
    assert manifest["stats"] == {"input_pairs": 38, "pairs": 36, "tasks": 17, "repos": 10}
    assert len(actual) == 36
    assert len({(repo, task) for repo, task, _ in actual}) == 17
    assert len({repo for repo, _, _ in actual}) == 10
    assert (directory / "pairs.txt").read_text().splitlines() == [
        f"{repo}:{task}:{pair[0]},{pair[1]}" for repo, task, pair in actual
    ]

import json

import identity


def vec(*xs):
    return identity.normalise(list(xs))


def test_classify_picks_the_clear_winner_and_admits_doubt():
    emb = {"a1": vec(1, 0, 0), "a2": vec(0.9, 0.1, 0), "b1": vec(0, 1, 0), "b2": vec(0.1, 0.9, 0)}
    gallery = [{"name": "Faff", "sighting": "a1"}, {"name": "Faff", "sighting": "a2"},
               {"name": "Siya", "sighting": "b1"}, {"name": "Siya", "sighting": "b2"}]
    assert identity.classify(vec(1, 0.05, 0), gallery, emb, ["Faff", "Siya"])[0] == "Faff"
    assert identity.classify(vec(1, 1, 0), gallery, emb, ["Faff", "Siya"])[0] is None      # halfway: unsure
    assert identity.classify(vec(0, 0, 1), gallery, emb, ["Faff", "Siya"])[0] is None      # looks like neither


def test_seeding_cleans_up_wrong_guesses():
    # The vision model called one Siya-looking cat "Faff"; clustering moves it to Siya.
    emb = {f"f{i}": vec(1, 0.05 * i, 0) for i in range(5)}
    emb.update({f"s{i}": vec(0.05 * i, 1, 0) for i in range(5)})
    emb["wrong"] = vec(0.02, 1, 0)
    records = {n: {"is_cat": True, "cats": [{"name": "Faff" if n.startswith("f") or n == "wrong" else "Siya"}]}
               for n in emb}
    gallery = identity.seed_gallery(records, emb, ["Faff", "Siya"], [])
    assert {e["sighting"] for e in gallery if e["name"] == "Siya"} >= {"wrong", "s0", "s4"}
    assert all(e["source"] == "auto" for e in gallery)


def test_fixes_win_and_relabel_records(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "DIR", tmp_path)
    monkeypatch.setattr(identity, "FIXES", tmp_path / "fixes")
    monkeypatch.setattr(identity, "GALLERY", tmp_path / "gallery.json")
    monkeypatch.setattr(identity, "EMBEDDINGS", tmp_path / "embeddings.json")
    monkeypatch.setattr(identity, "CROPS", tmp_path / "crops")
    emb = {f"f{i}": vec(1, 0.05 * i, 0) for i in range(4)}
    emb.update({f"s{i}": vec(0.05 * i, 1, 0) for i in range(4)})
    records = {n: {"is_cat": True, "cats": [{"name": "Faff", "appearance": "grey cat"}]} for n in emb}  # all guessed Faff
    ident = identity.Identity()
    ident.embeddings = dict(emb)
    (tmp_path / "fixes").mkdir()
    for i in range(2):
        (tmp_path / "fixes" / f"{i}.json").write_text(json.dumps({"sighting": f"s{i}", "cat": "Siya"}))
    ident.relabel(records, [{"name": "Faff"}, {"name": "Siya"}], reseed=True)
    assert records["s0"]["identity"]["method"] == "you"
    assert records["s3"]["cats"][0]["name"] == "Siya"      # learned from the two fixes
    assert records["f2"]["cats"][0]["name"] == "Faff"
    assert not list((tmp_path / "fixes").glob("*.json"))

"""Folder access: approved roots from the process maps, their genuine subfolders, and nothing else."""

from __future__ import annotations

import os

import pytest

from csv_o_matic import access
from csv_o_matic.csvscan import ScanControl
from csv_o_matic.service import ToolFailure

from conftest import digest, make_service

A = "stage/a"
SECRET = "ID,Secret\n00123,TOPSECRET\n"


@pytest.fixture(params=["directory-relative opens", "fallback without them"])
def mode(request, monkeypatch):
    """Run each access test both ways: with openat-style confinement, and with the checked-path fallback
    used where the platform has none (Windows)."""
    if request.param.startswith("fallback"):
        monkeypatch.setattr(access, "HAVE_DIR_FD", False)
    return request.param


@pytest.fixture
def tree(env):
    """stage/a/2026/11/deep.csv inside an approved root; look-alikes and secrets outside every root."""
    data = env[0]
    deep = data / A / "2026" / "11"
    deep.mkdir(parents=True)
    (deep / "deep.csv").write_text("ID,Portfolio,Qty\n00123,P1,10\n00123,P2,20\n777,P1,1\n")
    (data / A / "2026" / "10").mkdir()
    (data / A / "archive").mkdir()
    for outside in ("stage/a_backup", "outside"):  # a prefix look-alike sibling, and an unrelated folder
        (data / outside).mkdir()
        (data / outside / "secret.csv").write_text(SECRET)
    return data


def kind(call) -> str:
    with pytest.raises(ToolFailure) as info:
        call()
    return info.value.kind


def test_a_nested_subfolder_is_discovered_listed_inspected_and_searched(tree, service, mode):
    before = digest(tree)
    top = service.list_folders(folder=A)
    assert top["parent"] == {"folder": A, "kind": "mapped_folder"} and top["listing_complete"]
    assert [(f["folder"], f["kind"], f["approved_root"]) for f in top["folders"]] == [
        ("stage/a/2026", "discovered_subfolder", A), ("stage/a/archive", "discovered_subfolder", A)]
    assert [f["folder"] for f in service.list_folders(folder="stage/a/2026")["folders"]] == ["stage/a/2026/10", "stage/a/2026/11"]
    assert service.list_folders()["folders"][0]["kind"] == "mapped_folder"  # roots are told apart from discoveries

    sub = "stage/a/2026/11"
    files = service.list_csv_files(sub)
    assert [f["filename"] for f in files["files"]] == ["deep.csv"]
    assert files["folder_access"]["kind"] == "discovered_subfolder" and files["folder_access"]["approved_root"] == A
    assert files["filename_conventions_inherited_from"] == A and files["files"][0]["filename_status"] == "unparsed"
    # A single-folder listing stays a single folder: the parent does not list the child's files, it says they exist.
    parent = service.list_csv_files(A)
    assert "deep.csv" not in [f["filename"] for f in parent["files"]] and parent["subfolders"] == 2

    assert service.inspect_csv_headers(sub, "deep.csv")["columns"] == ["ID", "Portfolio", "Qty"]
    assert service.search_column_names(sub, "deep.csv", "port")["matching_column_count"] == 1
    found = service.find_security_in_file(ScanControl(), sub, "deep.csv", "00123", "ID")
    assert found["outcome"] == "match_found" and found["matches_returned"] == 2
    assert found["file"]["folder"] == sub and found["file"]["folder_access"]["approved_root"] == A
    assert service.count_matching_rows(ScanControl(), sub, "deep.csv", "00123", "ID")["count"] == 2
    assert service.search_across_columns(ScanControl(), sub, "deep.csv", "P2")["matches_returned"] == 1
    page = service.find_security_in_file(ScanControl(), sub, "deep.csv", "00123", "ID", max_rows=1)
    rest = service.find_security_in_file(ScanControl(), sub, "deep.csv", "00123", "ID", cursor=page["next_cursor"])
    assert rest["outcome"] == "match_found" and rest["more_matches"] == "none"
    both = service.compare_security_records(ScanControl(), sub, "deep.csv", "stage/b", "left.csv", "777", "ID",
                                            identifier_b="00777", compare_columns=["Qty"])
    assert both["comparison_status"] == "compared"
    # An absolute spelling is accepted only because it lies inside an approved root.
    absolute = str(tree / A / "2026" / "11")
    assert service.count_matching_rows(ScanControl(), absolute, "deep.csv", "777", "ID")["count"] == 1
    assert digest(tree) == before


@pytest.mark.parametrize("folder", [
    "/etc", "/", "stage", ".", "..", "stage/a_backup", "stage/a/../a_backup", "stage/a/../../outside",
    "stage/a/2026/../../../outside", "outside", "stage/a/2026/11/../../../../outside", "", "stage/a\x00/x",
])
def test_everything_outside_the_approved_roots_is_refused(tree, service, mode, folder):
    for call in (lambda: service.list_csv_files(folder),
                 lambda: service.inspect_csv_headers(folder, "secret.csv"),
                 lambda: service.search_across_columns(ScanControl(), folder, "secret.csv", "00123"),
                 lambda: service.count_matching_rows(ScanControl(), folder, "secret.csv", "00123", "ID"),
                 lambda: service.compare_security_records(ScanControl(), A, "many.csv", folder, "secret.csv", "DUP", "ID")):
        assert kind(call) == "folder_not_found"
    if folder:
        assert kind(lambda: service.list_folders(folder=folder)) == "folder_not_found"


def test_absolute_paths_outside_and_prefix_lookalikes_are_refused(tree, service, mode):
    for folder in (str(tree / "outside"), str(tree / "stage" / "a_backup"), str(tree), str(tree / "stage"),
                   str(tree / A) + "_backup", str(tree / A / ".." / "a_backup")):
        assert kind(lambda: service.list_csv_files(folder)) == "folder_not_found"
        assert kind(lambda: service.search_across_columns(ScanControl(), folder, "secret.csv", "00123")) == "folder_not_found"


def test_a_path_that_returns_inside_a_root_names_that_root(tree, service, mode):
    assert service.list_csv_files("stage/a/../b")["folder"] == "stage/b"  # spelled oddly, but it IS a mapped folder


def test_linked_directories_are_never_a_way_out(tree, service, mode):
    os.symlink(tree / "outside", tree / A / "escape")                 # a link directly inside a root
    os.symlink(tree / "outside", tree / A / "2026" / "11" / "deeper")  # ... and one further down
    os.symlink(tree / A / "archive", tree / A / "alias")              # even a link that stays inside is not followed
    listing = service.list_folders(folder=A)
    assert [f["name"] for f in listing["folders"]] == ["2026", "archive"]
    assert listing["linked_folders_not_followed"] == 2 and "does not show that nothing else exists" in listing["listing_warning"]
    for folder in ("stage/a/escape", "stage/a/2026/11/deeper", "stage/a/alias", str(tree / A / "escape")):
        for call in (lambda: service.list_csv_files(folder),
                     lambda: service.list_folders(folder=folder),
                     lambda: service.inspect_csv_headers(folder, "secret.csv"),
                     lambda: service.search_across_columns(ScanControl(), folder, "secret.csv", "00123"),
                     lambda: service.compare_security_records(ScanControl(), folder, "secret.csv", A, "many.csv", "00123", "ID")):
            assert kind(call) == "linked_folder_not_followed"
    # Below a link nothing is reachable either.
    (tree / "outside" / "inner").mkdir()
    (tree / "outside" / "inner" / "secret.csv").write_text(SECRET)
    assert kind(lambda: service.list_csv_files("stage/a/escape/inner")) == "linked_folder_not_followed"


def test_file_links_are_refused_in_subfolders_too(tree, service, mode):
    os.symlink(tree / "outside" / "secret.csv", tree / A / "2026" / "11" / "sneaky.csv")
    assert "sneaky.csv" not in [f["filename"] for f in service.list_csv_files("stage/a/2026/11")["files"]]
    assert kind(lambda: service.search_across_columns(ScanControl(), "stage/a/2026/11", "sneaky.csv", "00123")) == "file_unreadable"


def swap_for_link(tree, when: str, monkeypatch):
    """Replace stage/a/2026 by a link to an outside folder holding 2026-like content, at a chosen moment."""
    evil = tree / "outside" / "11"
    evil.mkdir()
    (evil / "deep.csv").write_text("ID,Portfolio,Qty\n00123,TOPSECRET,99\n")
    real_open, real_lstat = os.open, os.lstat
    state = {"done": False}

    def swap():
        if not state["done"]:
            state["done"] = True
            os.rename(tree / A / "2026", tree / A / "2026.moved")
            os.symlink(tree / "outside", tree / A / "2026")

    def racing_open(path, flags, *args, **kwargs):
        name = os.path.basename(str(path))
        if (when == "before the directory is opened" and name == "2026") or (when == "before the file is opened" and name == "deep.csv"):
            swap()
        return real_open(path, flags, *args, **kwargs)

    def racing_lstat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if when == "before the directory is opened" and os.path.basename(str(path)) == "2026":
            swap()  # the fallback checks with lstat: swap right after the check passes
        return result

    monkeypatch.setattr(access.os, "open", racing_open)
    if not access.HAVE_DIR_FD:
        monkeypatch.setattr(access.os, "lstat", racing_lstat)
    return state


@pytest.mark.parametrize("when", ["before the directory is opened", "before the file is opened"])
def test_swapping_an_intermediate_directory_during_access_exposes_nothing(tree, service, mode, monkeypatch, when):
    state = swap_for_link(tree, when, monkeypatch)
    try:
        out = service.find_security_in_file(ScanControl(), "stage/a/2026/11", "deep.csv", "00123", "ID")
    except ToolFailure as failure:
        out = {"refused": failure.kind}
    assert state["done"], "the race was not exercised"
    assert "TOPSECRET" not in str(out)
    if "refused" in out:  # fail closed ...
        assert out["refused"] in ("linked_folder_not_followed", "file_unreadable", "folder_not_found")
    else:  # ... or the genuine file, reached through the directory that was really opened
        assert access.HAVE_DIR_FD and [m["values"]["Portfolio"] for m in out["matches"]] == ["P1", "P2"]
    if not access.HAVE_DIR_FD:
        assert "refused" in out


def test_an_explicitly_mapped_nested_folder_uses_its_own_format_not_its_parents(tree, env, service, mode):
    nested = tree / A / "semis"
    (nested / "2026").mkdir(parents=True)
    for folder in (nested, nested / "2026"):
        (folder / "s.csv").write_text("id;name\nX1;a,b\n")
    (env[1] / "nested.yaml").write_text(
        "id: nested\nname: Nested\ndescription: d\npurpose: p\nrun_semantics: r\n"
        "folders:\n  - {id: semis, path: stage/a/semis, description: Semicolon files inside stage a, csv_format: {delimiter: ';'}}\n"
        "steps:\n  - {id: s, name: s, description: s, writes: [semis]}\n")
    child = next(f for f in service.list_folders(folder=A)["folders"] if f["name"] == "semis")
    assert child["kind"] == "mapped_folder" and child["folder"] == "stage/a/semis" and "approved_root" not in child
    out = service.find_security_in_file(ScanControl(), "stage/a/semis", "s.csv", "X1", "id")
    assert out["matches"][0]["values"] == {"id": "X1", "name": "a,b"}  # read with ';', the nested folder's own format
    # Its own subfolders inherit from it, the most specific root - not from stage/a.
    deeper = service.find_security_in_file(ScanControl(), "stage/a/semis/2026", "s.csv", "X1", "id")
    assert deeper["matches"][0]["values"]["name"] == "a,b" and deeper["file"]["folder_access"]["approved_root"] == "stage/a/semis"
    assert service.list_csv_files("stage/a/semis/2026").get("filename_conventions_inherited_from") is None  # none to inherit
    # A sibling subfolder of the parent still reads with the parent's comma format.
    assert service.inspect_csv_headers("stage/a/2026/11", "deep.csv")["column_count"] == 3


def test_subfolder_listings_are_paged_and_bounded(tree, env, mode):
    for i in range(35):
        (tree / A / "archive" / f"run_{i:03d}").mkdir()
        (tree / A / "archive" / f"run_{i:03d}" / "inner").mkdir()  # never read to produce the parent's listing
    service = make_service(env, max_files_per_listing=10)
    seen, offset, pages = [], 0, 0
    while offset is not None:
        page = service.list_folders(folder="stage/a/archive", offset=offset)
        assert page["total_folders"] == 35 and page["folders_returned"] <= 10
        seen += [f["name"] for f in page["folders"]]
        offset = page.get("next_offset")
        pages += 1
        assert page["truncated"] == (offset is not None)
    assert seen == [f"run_{i:03d}" for i in range(35)] and pages == 4


def test_an_empty_or_missing_subfolder_is_reported_as_what_it_is(tree, service, mode):
    empty = service.list_folders(folder="stage/a/2026/10")
    assert empty["folders"] == [] and empty["listing_complete"] and "listing_warning" not in empty
    assert service.list_csv_files("stage/a/2026/10")["total_matching_files"] == 0
    assert kind(lambda: service.list_csv_files("stage/a/2027")) == "folder_not_found"
    assert kind(lambda: service.list_csv_files("stage/a/many.csv")) == "folder_not_found"  # a file is not a folder


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="running as a user that ignores file permissions")
def test_an_unreadable_subfolder_is_an_error_not_an_empty_folder(tree, service):
    locked = tree / A / "archive"
    locked.chmod(0)
    try:
        assert kind(lambda: service.list_csv_files("stage/a/archive")) == "folder_unreadable"
        assert kind(lambda: service.list_folders(folder="stage/a/archive")) == "folder_unreadable"
    finally:
        locked.chmod(0o755)


def test_no_tool_can_widen_the_approved_roots(env, service):
    """The roots are whatever the process maps say when a request arrives; no service method writes anything."""
    import inspect

    import csv_o_matic.access as access_module
    import csv_o_matic.service as service_module
    for module in (service_module, access_module):
        source = inspect.getsource(module)
        for writer in ("os.O_WRONLY", "os.O_RDWR", "os.O_CREAT", "os.O_APPEND", "write_text", "write_bytes", ".write(",
                       "os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir", "os.makedirs", "shutil"):
            assert writer not in source, f"{module.__name__} must not contain {writer}"


# ---------------------------------------------------------------- filename conventions shared between maps

def shared_map(name: str, prefix: str, extra: str = "") -> str:
    return (f"id: {name}\nname: {name}\ndescription: d\npurpose: p\nrun_semantics: r\n"
            "folders:\n  - id: shared\n    path: shared\n    description: Shared by two processes.\n"
            "    filename_convention:\n"
            f"      pattern: \"{prefix}_{{business_date}}.csv\"\n"
            f"      regex: '^{prefix}_(?P<business_date>\\d{{8}})\\.csv$'\n"
            f"      tokens:\n        business_date: {{format: \"%Y%m%d\", meaning: \"As-of date ({name})\"}}\n{extra}"
            f"steps:\n  - {{id: s, name: s, description: s, writes: [shared]}}\n")


@pytest.fixture
def shared(env):
    folder = env[0] / "shared"
    (folder / "2026").mkdir(parents=True)
    for name in ("a_20261115.csv", "b_20261115.csv", "a_20261101.csv", "b_20261101.csv", "notes.csv"):
        (folder / name).write_text("ID\n1\n")
    (folder / "2026" / "b_20261115.csv").write_text("ID\n1\n")
    (env[1] / "one.yaml").write_text(shared_map("alpha", "a"))
    (env[1] / "two.yaml").write_text(shared_map("beta", "b"))
    return folder


def test_each_file_is_read_with_whichever_convention_matches_it(shared, env, service):
    listed = service.list_csv_files("shared", date_from="2026-11-15", date_to="2026-11-15")
    assert sorted(f["filename"] for f in listed["files"]) == ["a_20261115.csv", "b_20261115.csv"]
    assert all(f["filename_tokens"] == {"business_date": "2026-11-15"} and f["filename_status"] == "parsed" for f in listed["files"])
    # notes.csv has no date: it is excluded for that reason, which is reported separately from "out of range".
    assert listed["files_excluded_because_filename_has_no_parseable_date"] == 1 and "NOT because" in listed["date_exclusion_note"]
    assert {c["pattern"]: c["declared_by"] for c in listed["filename_conventions"]} == {
        "a_{business_date}.csv": ["alpha"], "b_{business_date}.csv": ["beta"]}
    everything = service.list_csv_files("shared")
    assert next(f for f in everything["files"] if f["filename"] == "notes.csv")["filename_status"] == "unparsed"
    assert everything["total_matching_files"] == 5
    # Subfolders of the shared root are governed by the same reconciled conventions.
    below = service.list_csv_files("shared/2026", date_from="2026-11-15")
    assert [f["filename"] for f in below["files"]] == ["b_20261115.csv"]

    # Renaming the map files reverses their loading order and changes nothing.
    os.rename(env[1] / "one.yaml", env[1] / "zzz.yaml")
    os.rename(env[1] / "two.yaml", env[1] / "aaa.yaml")
    assert service.list_csv_files("shared", date_from="2026-11-15", date_to="2026-11-15") == listed
    assert service.list_csv_files("shared") == everything


def test_equivalent_conventions_combine_and_conflicting_ones_are_ambiguous(shared, env, service):
    (env[1] / "three.yaml").write_text(shared_map("gamma", "a"))  # the same convention as alpha
    listed = service.list_csv_files("shared", filename_contains="a_20261115")
    assert listed["files"][0]["filename_status"] == "parsed"
    assert {tuple(c["declared_by"]) for c in listed["filename_conventions"]} == {("alpha", "gamma"), ("beta",)}
    # delta reads the same names day-first: 'a_20111202' is 2011-12-02 to alpha and 1202-11-20 to delta.
    (env[1] / "four.yaml").write_text(shared_map("delta", "a").replace("%Y%m%d", "%d%m%Y"))
    (shared / "a_20111202.csv").write_text("ID\n1\n")
    item = next(f for f in service.list_csv_files("shared")["files"] if f["filename"] == "a_20111202.csv")
    assert item["filename_status"] == "ambiguous" and item["filename_tokens"] is None
    assert sorted(i["tokens"]["business_date"] for i in item["filename_interpretations"]) == ["1202-11-20", "2011-12-02"]
    dated = service.list_csv_files("shared", date_from="2011-01-01", date_to="2011-12-31")
    assert dated["files"] == [] and dated["files_excluded_because_filename_date_is_ambiguous"] >= 1
    # a_20261115 cannot be a day-first date, so only one reading exists and it stays usable.
    assert "a_20261115.csv" in [f["filename"] for f in service.list_csv_files("shared", date_from="2026-11-15")["files"]]


def test_disagreeing_default_date_fields_must_be_resolved_by_the_caller(shared, env, service):
    (env[1] / "two.yaml").write_text(
        shared_map("beta", "b").replace("business_date", "as_of"))
    with pytest.raises(ToolFailure) as info:
        service.list_csv_files("shared", date_from="2026-11-15")
    assert info.value.kind == "ambiguous_date_field" and "as_of" in info.value.message and "business_date" in info.value.message
    by_a = service.list_csv_files("shared", date_from="2026-11-15", date_field="business_date")
    assert [f["filename"] for f in by_a["files"]] == ["a_20261115.csv"]
    assert by_a["files_excluded_because_filename_has_no_parseable_date"] == 3  # the b_ files and notes.csv: no such token
    by_b = service.list_csv_files("shared", date_from="2026-11-15", date_field="as_of")
    assert [f["filename"] for f in by_b["files"]] == ["b_20261115.csv"]


def test_conflicting_csv_formats_are_still_refused_whatever_the_filename(shared, env, service):
    (env[1] / "two.yaml").write_text(shared_map("beta", "b").replace(
        "    description: Shared by two processes.\n", "    description: x\n    csv_format: {delimiter: ';'}\n"))
    (env[1] / "one.yaml").write_text(shared_map("alpha", "a").replace(
        "    description: Shared by two processes.\n", "    description: x\n    csv_format: {delimiter: ','}\n"))
    for name in ("a_20261115.csv", "b_20261115.csv"):  # the format is never guessed from which convention matched
        with pytest.raises(ToolFailure) as info:
            service.inspect_csv_headers("shared", name)
        assert info.value.kind == "process_map_conflict"

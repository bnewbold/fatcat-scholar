import difflib
from typing import Any, Dict, List, Tuple, TypeAlias
from uuid import UUID

import fatcat_openapi_client as fcapi
from fatcat_openapi_client import (
    ContainerEntity,
    CreatorEntity,
    Editgroup,
    EntityEdit,
    FileEntity,
    FilesetEntity,
    ReleaseEntity,
    ReleaseExtIds,
    WebcaptureEntity,
    WorkEntity,
)
from scholar.cat.tools.transforms.elasticsearch import (
    container_to_elasticsearch,
    file_to_elasticsearch,
    release_to_elasticsearch
)
from scholar.cat.tools.transforms.entities import (
    entity_to_toml,
)
from scholar.cat.hacks import strip_extlink_xml, wayback_suffix

Entity: TypeAlias = ContainerEntity|CreatorEntity|FileEntity|FilesetEntity|ReleaseEntity|WebcaptureEntity|WorkEntity

def enrich_container_entity(entity: ContainerEntity) -> ContainerEntity:
    if entity.state in ("redirect", "deleted"):
        return entity
    if entity.state == "active":
        entity._es = container_to_elasticsearch(entity, force_bool=False)
    return entity

def enrich_file_entity(entity: FileEntity) -> FileEntity:
    if entity.state == "active":
        entity._es = file_to_elasticsearch(entity)
    return entity

def enrich_creator_entity(fcclient: fcapi.DefaultApi, entity: CreatorEntity) -> CreatorEntity:
    if entity.state in ("redirect", "deleted"):
        return entity
    entity._releases = None
    if entity.state in ("active", "wip"):
        entity._releases = fcclient.get_creator_releases(entity.ident)
    return entity

def enrich_release_entity(entity: ReleaseEntity) -> ReleaseEntity:
    if entity.state in ("redirect", "deleted"):
        return entity
    if entity.state == "active":
        entity._es = release_to_elasticsearch(entity, force_bool=False)
    if entity.container and entity.container.state == "active":
        entity.container._es = container_to_elasticsearch(entity.container, force_bool=False)
    if entity.files:
        # remove shadows-only files with no URLs
        entity.files = [
            f for f in entity.files if not (f.extra and f.extra.get("shadows") and not f.urls)
        ]
    if entity.filesets:
        for fs in entity.filesets:
            fs._total_size = sum([f.size for f in fs.manifest])
    if entity.webcaptures:
        for wc in entity.webcaptures:
            wc._wayback_suffix = wayback_suffix(wc)
    for ref in entity.refs:
        # this is a UI hack to get rid of XML crud in unstructured refs like:
        # LOCKSS (2014) Available: <ext-link
        # xmlns:xlink="http://www.w3.org/1999/xlink" ext-link-type="uri"
        # xlink:href="http://lockss.org/"
        # xlink:type="simple">http://lockss.org/</ext-link>. Accessed: 2014
        # November 1.
        if ref.extra and ref.extra.get("unstructured"):
            ref.extra["unstructured"] = strip_extlink_xml(ref.extra["unstructured"])
    # for backwards compatibility, copy extra['subtitle'] to subtitle
    if not entity.subtitle and entity.extra and entity.extra.get("subtitle"):
        if isinstance(entity.extra["subtitle"], str):
            entity.subtitle = entity.extra["subtitle"]
        elif isinstance(entity.extra["subtitle"], list):
            entity.subtitle = entity.extra["subtitle"][0] or None
    # author list to display; ensure it's sorted by index (any othors with
    # index=None go to end of list)
    authors = [
        c
        for c in entity.contribs
        if c.role in ("author", None)
        and (c.surname or c.raw_name or (c.creator and c.creator.surname))
    ]
    entity._authors = sorted(authors, key=lambda c: (c.index is None and 99999999) or c.index)
    # need authors, title for citeproc to work
    entity._can_citeproc = bool(entity._authors) and bool(entity.title)
    if entity.abstracts and entity.abstracts[0].mimetype:
        # hack to show plain text instead of latex abstracts
        if "latex" in entity.abstracts[0].mimetype:
            entity.abstracts.reverse()
        # hack to (partially) clean up common JATS abstract display case
        if entity.abstracts[0].mimetype == "application/xml+jats":
            for tag in ("p", "jats", "jats:p"):
                entity.abstracts[0].content = entity.abstracts[0].content.replace(
                    "<{}>".format(tag), ""
                )
                entity.abstracts[0].content = entity.abstracts[0].content.replace(
                    "</{}>".format(tag), ""
                )
                # ugh, double encoding happens
                entity.abstracts[0].content = entity.abstracts[0].content.replace(
                    "&lt;/{}&gt;".format(tag), ""
                )
                entity.abstracts[0].content = entity.abstracts[0].content.replace(
                    "&lt;{}&gt;".format(tag), ""
                )
    return entity

def enrich_fileset_entity(entity: FilesetEntity) -> FilesetEntity:
    if entity.state in ("redirect", "deleted"):
        return entity
    entity._total_size = None
    if entity.manifest is not None:
        entity._total_size = sum([f.size for f in entity.manifest]) or 0
    return entity

def enrich_work_entity(fcclient: fcapi.DefaultApi, entity: WorkEntity) -> WorkEntity:
    if entity.state in ("redirect", "deleted"):
        return entity
    entity._releases = None
    if entity.state in ("active", "wip"):
        entity._releases = fcclient.get_work_releases(entity.ident)
    return entity

def enrich_webcapture_entity(entity: WebcaptureEntity) -> WebcaptureEntity:
    if entity.state in ("redirect", "deleted"):
        return entity
    entity._wayback_suffix = wayback_suffix(entity)
    return entity


def generic_get_entity(fcclient: fcapi.DefaultApi, entity_type: str, ident: str, enrich: bool = True) -> Entity:
    # TODO this originally put its body in a try and then used flask's .abort()
    # for api exceptions or ApiValueError; I'm just letting it bubble up.
    if entity_type == "container" and enrich:
        return enrich_container_entity(fcclient.get_container(ident))
    elif entity_type == "container":
        return fcclient.get_container(ident)
    elif entity_type == "creator" and enrich:
        return enrich_creator_entity(fcclient, fcclient.get_creator(ident))
    elif entity_type == "creator":
        return fcclient.get_creator(ident)
    elif entity_type == "file" and enrich:
        return enrich_file_entity(fcclient.get_file(ident, expand="releases"))
    elif entity_type == "file":
        return fcclient.get_file(ident, expand="releases")
    elif entity_type == "fileset" and enrich:
        return enrich_fileset_entity(fcclient.get_fileset(ident, expand="releases"))
    elif entity_type == "fileset":
        return fcclient.get_fileset(ident)
    elif entity_type == "webcapture" and enrich:
        return enrich_webcapture_entity(fcclient.get_webcapture(ident, expand="releases"))
    elif entity_type == "webcapture":
        return fcclient.get_webcapture(ident)
    elif entity_type == "release" and enrich:
        return enrich_release_entity(
            fcclient.get_release(ident, expand="container,creators,files,filesets,webcaptures")
        )
    elif entity_type == "release":
        return fcclient.get_release(ident)
    elif entity_type == "work" and enrich:
        return enrich_work_entity(fcclient, fcclient.get_work(ident))
    elif entity_type == "work":
        return fcclient.get_work(ident)
    else:
        raise NotImplementedError

def generic_get_entity_revision(fcclient: fcapi.DefaultApi, entity_type: str, revision_id: UUID, enrich: bool = True) -> Entity:
    # TODO just letting exceptions bubble up
    rev_id = str(revision_id)
    if entity_type == "container" and enrich:
        return enrich_container_entity(
                fcclient.get_container_revision(rev_id))
    elif entity_type == "container":
        return fcclient.get_container_revision(rev_id)
    elif entity_type == "creator" and enrich:
        return enrich_creator_entity(
                fcclient.get_creator_revision(rev_id))
    elif entity_type == "creator":
        return fcclient.get_creator_revision(rev_id)
    elif entity_type == "file" and enrich:
        return enrich_file_entity(
                fcclient.get_file_revision(rev_id, expand="releases"))
    elif entity_type == "file":
        return fcclient.get_file_revision(rev_id)
    elif entity_type == "fileset" and enrich:
        return enrich_fileset_entity(
            fcclient.get_fileset_revision(rev_id, expand="releases")
        )
    elif entity_type == "fileset":
        return fcclient.get_fileset_revision(rev_id)
    elif entity_type == "webcapture" and enrich:
        return enrich_webcapture_entity(
            fcclient.get_webcapture_revision(rev_id, expand="releases")
        )
    elif entity_type == "webcapture":
        return fcclient.get_webcapture_revision(rev_id)
    elif entity_type == "release" and enrich:
        return enrich_release_entity(
            fcclient.get_release_revision(rev_id, expand="container")
        )
    elif entity_type == "release":
        return fcclient.get_release_revision(rev_id)
    elif entity_type == "work" and enrich:
        return enrich_work_entity(fcclient, fcclient.get_work_revision(rev_id))
    elif entity_type == "work":
        return fcclient.get_work_revision(rev_id)
    else:
        raise NotImplementedError(f"entity_type: {entity_type}")

def generic_get_editgroup_entity(
    fcclient: fcapi.DefaultApi,
    editgroup: Editgroup,
    entity_type: str,
    ident: str,
    enrich: bool = True,
) -> Tuple[Entity, EntityEdit]:
    if entity_type == "container":
        edits = editgroup.edits.containers
    elif entity_type == "creator":
        edits = editgroup.edits.creators
    elif entity_type == "file":
        edits = editgroup.edits.files
    elif entity_type == "fileset":
        edits = editgroup.edits.filesets
    elif entity_type == "webcapture":
        edits = editgroup.edits.webcaptures
    elif entity_type == "release":
        edits = editgroup.edits.releases
    elif entity_type == "work":
        edits = editgroup.edits.works
    else:
        raise NotImplementedError
    revision_id = None
    edit = None
    for e in edits:
        if e.ident == ident:
            revision_id = e.revision
            edit = e
            break
    if not edit:
        # couldn't find relevant edit in this editgroup
        # TODO better error type that can map to 404 like before
        raise Exception("not found")
    if not revision_id:
        # deletion, presumably
        return generic_deleted_entity(entity_type, ident), edit

    entity = generic_get_entity_revision(fcclient, entity_type, revision_id, enrich=enrich)
    entity.ident = ident
    if edit.redirect_ident:
        entity.state = "redirect"
        entity.redirect = edit.redirect_ident
    elif edit.prev_revision:
        # TODO: this doesn't catch the case of "deleted but then undeleted" or
        # similar situations where edit.prev_revision is not set. Really we
        # should re-fetch from the API or something.
        entity.state = "active"
    else:
        entity.state = "wip"
    return entity, edit

def generic_deleted_entity(entity_type: str, ident: str) -> Entity:
    if entity_type == "container":
        entity = ContainerEntity()
    elif entity_type == "creator":
        entity = CreatorEntity()
    elif entity_type == "file":
        entity = FileEntity()
    elif entity_type == "fileset":
        entity = FilesetEntity()
    elif entity_type == "webcapture":
        entity = WebcaptureEntity()
    elif entity_type == "release":
        entity = ReleaseEntity(ext_ids=ReleaseExtIds())
    elif entity_type == "work":
        entity = WorkEntity()
    else:
        raise NotImplementedError
    entity.ident = ident
    entity.state = "deleted"
    return entity

def editgroup_get_diffs(fcclient: fcapi.DefaultApi, editgroup: Editgroup) -> Dict[str, Any]:
    """
    Fetches before/after entity revisions, and computes "diffs" of TOML representations.

    Returns a dict with entity type (pluralized, like "files"), then within
    that a dict with entity ident (without prefix) containing a list of
    strings, one per line of the "unified diff" format. If there is no diff for
    an edited entity (eg, it was or redirected), instead `None` is returned for
    that entity.
    """
    diffs: Dict[str, Any] = {}

    for entity_type in [
        "container",
        "creator",
        "release",
        "work",
        "file",
        "fileset",
        "webcapture",
    ]:
        edits = getattr(editgroup.edits, entity_type + "s") or []
        diffs[entity_type] = {}
        for ed in edits:
            # only for creation and update
            if ed.revision and not ed.redirect_ident:
                diffs[entity_type][ed.ident] = _entity_edit_diff(fcclient, entity_type, ed)
            else:
                diffs[entity_type][ed.ident] = None
    return diffs

def _entity_edit_diff(fcclient: fcapi.DefaultApi, entity_type: str, entity_edit: EntityEdit) -> List[str]:
    """
    Helper to generate diff lines for a single entity edit.

    Schema of entity_edit (as a reminder):

        entity_edit
            ident
            revision
            prev_revision
            redirect_ident
    """
    pop_fields = ["ident", "revision", "state"]
    new_rev = generic_get_entity_revision(fcclient, entity_type, entity_edit.revision, enrich=False)
    new_toml = entity_to_toml(new_rev,
                              api_client=fcclient.api_client,
                              pop_fields=pop_fields).strip().split("\n")
    if len(new_toml) == 1 and not new_toml[0].strip():
        new_toml = []
    if entity_edit.prev_revision:
        old_rev = generic_get_entity_revision(fcclient,
            entity_type, entity_edit.prev_revision, enrich=False
        )
        old_toml = entity_to_toml(old_rev,
                                  api_client=fcclient.api_client,
                                  pop_fields=pop_fields).strip().split("\n")
        fromdesc = f"/{entity_type}/rev/{entity_edit.prev_revision}.toml"
    else:
        old_toml = []
        fromdesc = "(created)"

    diff_lines = list(
        difflib.unified_diff(
            old_toml,
            new_toml,
            fromfile=fromdesc,
            tofile=f"/{entity_type}/rev/{entity_edit.revision}.toml",
        )
    )
    return diff_lines

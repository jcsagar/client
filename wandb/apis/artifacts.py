# TODO: come up with format for artifact manifest. Filenames can include all kinds of
# nasty characters, which is probably why git uses null char separation for fields

import collections
import hashlib
import json
import os
from six import string_types
import tempfile
import time

from wandb import InternalApi
from wandb.apis import fakeapi
from wandb.apis import artifact_manifest
from wandb.file_pusher import FilePusher
from wandb import util

ARTIFACT_METADATA_FILENAME = 'artifact-metadata.json'

# Like md5_file in util but not b64 encoded.
# TODO(artifacts): decide what we actually want


def hash_file(path):
    return util.md5_file(path)


class Artifact(object):
    @classmethod
    def from_alias(alias):
        # TODO
        # fetch alias from server
        pass

    @classmethod
    def from_files(paths):
        # TODO
        # create from files
        pass

    def __init__(self, digest):
        # all artifacts have a digest
        pass

    def download(self, paths=None):
        # TODO: return dir?
        pass

    @property
    def dir(self):
        pass

    # in public api
    #   download artifact, query artifacts, create artifact (user)
    # in run api
    #   use path:
    #     create an artifact from local files, set metadata, sync if we need to, log
    #         as input, use the files
    #     or load an artifact from the server, download, use the files, log as input


class ArtifactMetadata(object):
    @classmethod
    def from_file(cls, path):
        return cls(json.load(open(path), object_pairs_hook=collections.OrderedDict))

    def __init__(self, metadata):
        self._metadata = metadata

    @property
    def value(self):
        return self._metadata

    def dump(self, fp):
        json.dump(self._metadata, fp, sort_keys=True, allow_nan=False, indent=4)

    def digest(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as fp:
            self.dump(fp)
            fp.close()
            # TODO: what format for md5
            return hash_file(fp.name)


def user_paths_to_path_specs(paths):
    # path_specs maps from internal artifact path to local path on disk
    path_specs = {}
    if isinstance(paths, list):
        # every entry must be a file, and duplicate basenames are not allowed.
        # we don't check if they're valid files here. We'll do that later when we
        # checksum (so that we only have to do a big parallel operation in one place)
        for path in paths:
            basename = os.path.basename(path)
            if basename in path_specs:
                raise ValueError('Duplicate file name found in artifact file list. Pass a dictionary instead.')
            path_specs[basename] = path
    elif isinstance(paths, dict):
        # Cool
        path_specs = paths
    else:
        # We have a single file or directory
        # Note, we follow symlinks for files contained within the diretory
        path = paths
        if os.path.isdir(path):
            for dirpath, _, filenames in os.walk(path, followlinks=True):
                for fname in filenames:
                    local_path = os.path.join(dirpath, fname)
                    artifact_path = os.path.relpath(local_path, path)
                    path_specs[artifact_path] = local_path
        elif os.path.isfile(path):
            basename = os.path.basename(path)
            path_specs[basename] = path
        else:
            raise ValueError('paths must be a list, dictionary, or valid file or directory path.')
    if len(path_specs) == 0:
        raise ValueError('At least one valid path must be specified.')
    return path_specs


LocalArtifactEntry = collections.namedtuple('LocalArtifactEntry', ('path', 'hash', 'local_path'))


class LocalArtifactManifestV1(object):
    def __init__(self, paths, metadata=None):
        self._path_specs = user_paths_to_path_specs(paths)

        self._metadata = None
        if metadata is not None:
            self._metadata = ArtifactMetadata(metadata)
        if ARTIFACT_METADATA_FILENAME in self._path_specs:
            if self._metadata is not None:
                raise ValueError('may not specify metadata argument if paths contains %s' % ARTIFACT_METADATA_FILENAME)
            # pop it out of path specs
            local_path = self._path_specs.pop(ARTIFACT_METADATA_FILENAME)
            self._metadata = ArtifactMetadata.from_file(local_path)

        self._local_entries = []
        if self._metadata is not None:
            self._local_entries.append(
                LocalArtifactEntry(ARTIFACT_METADATA_FILENAME,
                                   self._metadata.digest(),
                                   None))  # use none for local path, this file gets directly encoded in the LocalArtifact file
        for artifact_path, local_path in self._path_specs.items():
            self._local_entries.append(
                LocalArtifactEntry(artifact_path, hash_file(local_path), os.path.abspath(local_path)))

        self._manifest = artifact_manifest.ArtifactManifestV1(self._local_entries)
        self._digest = self._manifest.digest

    @property
    def entries(self):
        return self._local_entries

    @property
    def digest(self):
        return self._digest

    @property
    def metadata(self):
        if self._metadata is None:
            return None
        return self._metadata.value

    def dump(self, fp):
        fp.write('version: 1\n')
        fp.write('digest: %s\n' % self._digest)
        fp.write('metadata: ')
        if self._metadata is not None:
            json.dump(self._metadata.value, fp)
        fp.write('\n')
        for entry in self._local_entries:
            if entry.path == ARTIFACT_METADATA_FILENAME:
                continue
            # TODO(artifacts): Filenames can have nasty chars, maybe each line is json
            fp.write('%s %s %s\n' % (entry.path, entry.local_path, entry.hash))


class LocalArtifact(object):
    # TODO: entity, project, file_pusher, api. Can we use some kind of context
    # thing?
    def __init__(self, api, paths, metadata=None, file_pusher=None, is_user_created=False):
        self._api = api
        # TODO(artifacts): move file_pusher inside API.
        self._file_pusher = file_pusher
        if self._file_pusher is None:
            self._file_pusher = FilePusher(self._api)
        self._is_user_created = is_user_created
        self._local_manifest = LocalArtifactManifestV1(paths, metadata=metadata)
        self._server_artifact = None

    def save(self, name, description=None, aliases=None, labels=None):
        """Returns the server artifact."""
        artifact_id = self._api.create_artifact(name)
        # TODO(artifacts), send aliases, labels, description
        self._server_artifact = self._api.create_artifact_version(
            artifact_id, self._local_manifest.digest,
            metadata=json.dumps(self._local_manifest.metadata),
            is_user_created=self._is_user_created)
        if self._server_artifact['state'] == 'COMMITTED':
            # TODO: update aliases, labels, description etc?
            return self._server_artifact
        elif self._server_artifact['state'] != 'PENDING':
            # TODO: what to do in this case?
            raise Exception('Server artifact not in PENDING state')
        for entry in self._local_manifest.entries:
            # Sync files. Because of hacking this on the client-side we may have multiple runs
            # pushing to the same artifact version. We could fix this on the server (or maybe it's not
            # the worst thing?)
            if entry.path == ARTIFACT_METADATA_FILENAME:
                metadata_file = self._file_pusher.named_temp_file(mode='w+')
                self._local_manifest._metadata.dump(metadata_file)
                metadata_file.close()
                self._file_pusher.file_changed(entry.path, metadata_file.name, self._server_artifact['id'])
            else:
                self._file_pusher.file_changed(entry.path, entry.local_path, self._server_artifact['id'])
        self._file_pusher.commit_artifact(self._server_artifact['id'])
        return self._server_artifact

    def commit(self):
        self._api.commit_artifact_version(self._server_artifact.id)

    def wait(self):
        if self._server_artifact is None:
            raise ValueError('Must call save first')
        while self._server_artifact.state != 'READY':
            time.sleep(2)


class LocalArtifactRead(object):
    def __init__(self, name, path):
        if not isinstance(path, string_types):
            raise ValueError("path must be a local file or directory")
        if os.path.isdir(path):
            self._artifact_dir = path
        elif os.path.isfile(path):
            self._artifact_dir = os.path.dirname(path)
        else:
            raise ValueError("path must be a local file or directory")
        self._local_manifest = LocalArtifactManifestV1(path)

    # TODO: give some way for the user to check if we have this on the server?

    @property
    def digest(self):
        return self._local_manifest.digest()

    def download(self, *args, **kwargs):
        return self._artifact_dir

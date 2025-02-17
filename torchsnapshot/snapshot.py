#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import asyncio

import copy
import fnmatch
import functools
import itertools
import logging
import os
import sys
import traceback

from collections import defaultdict
from datetime import timedelta
from threading import Thread
from typing import Any, Callable, cast, Dict, List, Optional, Set, Tuple, TypeVar

import torch
import torch.distributed as dist
from torch.distributed._shard.sharded_tensor import ShardedTensor
from torch.nn.parallel import DistributedDataParallel as DDP

from .batcher import batch_read_requests, batch_write_requests

from .dist_store import get_or_create_store, LinearBarrier

from .flatten import flatten, inflate
from .io_preparer import prepare_read, prepare_write
from .io_types import ReadIO, ReadReq, StoragePlugin, WriteIO, WriteReq
from .knobs import is_batching_disabled

from .manifest import (
    Entry,
    is_container_entry,
    Manifest,
    PrimitiveEntry,
    SnapshotMetadata,
)
from .manifest_ops import get_manifest_for_rank, handle_sharded_tensor_elasticity
from .partitioner import consolidate_replicated_entries, partition_write_reqs
from .pg_wrapper import PGWrapper
from .rng_state import RNGState
from .scheduler import (
    _MAX_PER_RANK_MEMORY_BUDGET_BYTES,
    get_process_memory_budget_bytes,
    PendingIOWork,
    sync_execute_read_reqs,
    sync_execute_write_reqs,
)
from .stateful import AppState, Stateful
from .storage_plugin import url_to_storage_plugin_in_event_loop
from .version import __version__ as torchsnapshot_version

logger: logging.Logger = logging.getLogger(__name__)

SNAPSHOT_METADATA_FNAME = ".snapshot_metadata"
T = TypeVar("T")


class Snapshot:
    """
    Snapshot represents the persisted program state at one point in time.

    Basic usage:
    ::

        # Define the program state
        app_state = {"model": model, "optimizer": optimizer"}

        # At an appropriate time, persist the program state as a snapshot
        snapshot = Snapshot.take(path=path, app_state=app_state)

        # On resuming, restore the program state from a snapshot
        snapshot.restore(app_state)

    Overview:

        At high level, torchsnapshot saves each value in state dicts as a
        file/object in the corresponding storage system. It also saves a manifest
        describing the persisted values and the structure of the original state
        dict.

        Comparing with :py:func:`torch.save` and :py:func:`torch.load`, torchsnapshot:

        - Enables efficient random access of persisted model weights.

        - Accelerates persistence by parallelizing writes.

            - For replicated values, persistence is parallelized across ranks.

        - Enables flexible yet robust elasticity (changing world size on
          restore).


    Elasticity:

        Elasticity is implemented via correctly making persisted values
        available to a newly joined rank, and having it correctly restores the
        corresponding runtime objects from the persisted values.

        For the purpose of elasticity, all persisted values fall into one of
        the categories in [per-rank, replicated, sharded].

        per-rank:

            By default, all non-sharded values are treated as per-rank.

            On save, the value is only saved by the owning rank.

            On load, the value is only made available to the same rank.

        replicated:

            A user can suggest any non-sharded value as replicated via glob
            patterns.

            On save, the value is only saved once (can be by any rank).

            On load, the value is made available to all ranks, including newly
            joined ranks.

        sharded:

            Specific types are always treated as sharded (e.g. ShardedTensor).

            On save, all shard-owning ranks save their shards.

            On load, all shards are made available to all ranks, including
            newly joined rank. All ranks can read from all shards for
            restoring the runtime object from persisted values.
            (ShardedTensor resharding is powered by torch.dist.checkpoint).

        If all values within a snapshot are either replicated or sharded, the
        snapshot is automatically reshard-able.

        If a snapshot contains per-rank values, it cannot be resharded unless
        the per-rank values are explicitly coerced to replicated on load.
    """

    def __init__(
        self,
        path: str,
        pg: Optional[dist.ProcessGroup] = None,
        storage_options: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Initializes the reference to an existing snapshot.

        Args:
            path: The location of the snapshot.
            pg: The process group for the processes restoring from the snapshot.
                When unspecified:
                    - If distributed is initialized, the global process group will be used.
                    - If distributed is not initialized, single process is assumed.
            storage_options: Additional keyword options for the StoragePlugin to use.
                See each StoragePlugin's documentation for customizations.
        """
        self.path: str = path
        self.pg: Optional[dist.ProcessGroup] = pg
        self._metadata: Optional[SnapshotMetadata] = None
        self._storage_options = storage_options

    @classmethod
    def take(
        cls,
        path: str,
        app_state: AppState,
        pg: Optional[dist.ProcessGroup] = None,
        replicated: Optional[List[str]] = None,
        storage_options: Optional[Dict[str, Any]] = None,
        _custom_tensor_prepare_func: Optional[
            Callable[[str, torch.Tensor, bool], torch.Tensor]
        ] = None,
    ) -> "Snapshot":
        """
        Take a snapshot from the program state.

        Args:
            app_state: The program state to take the snapshot from.
            path: The location to save the snapshot.
            pg: The process group for the processes taking the snapshot.
            When unspecified:
                    - If distributed is initialized, the global process group will be used.
                    - If distributed is not initialized, single process is assumed.
            replicated: A list of glob patterns for hinting the matching paths
                as replicated. Note that patterns not specified by all ranks
                are ignored.
            storage_options: Additional keyword options for the StoragePlugin to use.
                See each StoragePlugin's documentation for customizations.

        Returns:
            The newly taken snapshot.
        """
        torch._C._log_api_usage_once("torchsnapshot.Snapshot.take")
        cls._validate_app_state(app_state)

        event_loop = asyncio.new_event_loop()
        pg_wrapper = PGWrapper(pg=pg)

        path, coalesced_replicated = cls._coalesce_path_and_replicated(
            path=path,
            pg_wrapper=pg_wrapper,
            app_state=app_state,
            replicated=replicated or [],
        )
        storage = url_to_storage_plugin_in_event_loop(
            url_path=path, event_loop=event_loop, storage_options=storage_options
        )
        pending_io_work, metadata = cls._take_impl(
            path=path,
            app_state=app_state,
            replicated=coalesced_replicated,
            pg_wrapper=PGWrapper(pg),
            storage=storage,
            event_loop=event_loop,
            is_async_snapshot=False,
            _custom_tensor_prepare_func=_custom_tensor_prepare_func,
        )
        pending_io_work.sync_complete(event_loop=event_loop)

        # IMPORTANT: commit snapshot metadata only after all ranks complete writing
        pg_wrapper.barrier()
        if pg_wrapper.get_rank() == 0:
            cls._write_snapshot_metadata(
                snapshot_metadata=metadata,
                storage=storage,
                event_loop=event_loop,
            )

        storage.sync_close(event_loop=event_loop)
        event_loop.close()
        snapshot = cls(path=path, pg=pg, storage_options=storage_options)
        snapshot._metadata = metadata
        return snapshot

    @classmethod
    def async_take(
        cls,
        path: str,
        app_state: AppState,
        pg: Optional[dist.ProcessGroup] = None,
        replicated: Optional[List[str]] = None,
        storage_options: Optional[Dict[str, Any]] = None,
        _custom_tensor_prepare_func: Optional[
            Callable[[str, torch.Tensor, bool], torch.Tensor]
        ] = None,
    ) -> "PendingSnapshot":
        """
        Asynchronously take a snapshot from the program state.

        This method creates a consistent snapshot of the app state (i.e.
        changes to the app state after this method returns have no effect on
        the snapshot). The asynchronicity is a result of performing storage I/O
        in the background.

        Args:
            app_state: The program state to take the snapshot from.
            path: The location to save the snapshot.
            pg: The process group for the processes taking the snapshot.
            When unspecified:
                    - If distributed is initialized, the global process group will be used.
                    - If distributed is not initialized, single process is assumed.
            replicated: A list of glob patterns for hinting the matching paths
                as replicated. Note that patterns not specified by all ranks
                are ignored.
            storage_options: Additional keyword options for the StoragePlugin to use.
                See each StoragePlugin's documentation for customizations.

        Returns:
            A handle with which the newly taken snapshot can be obtained via
            `.wait()`. Note that waiting on the handle is optional. The
            snapshot will be committed regardless of whether `.wait()` is
            invoked.
        """
        torch._C._log_api_usage_once("torchsnapshot.Snapshot.async_take")
        cls._validate_app_state(app_state)

        event_loop = asyncio.new_event_loop()
        pg_wrapper = PGWrapper(pg=pg)
        path, coalesced_replicated = cls._coalesce_path_and_replicated(
            path=path,
            pg_wrapper=pg_wrapper,
            app_state=app_state,
            replicated=replicated or [],
        )
        storage = url_to_storage_plugin_in_event_loop(
            url_path=path, event_loop=event_loop, storage_options=storage_options
        )

        pending_io_work, metadata = cls._take_impl(
            path=path,
            app_state=app_state,
            replicated=coalesced_replicated,
            pg_wrapper=PGWrapper(pg),
            storage=storage,
            event_loop=event_loop,
            is_async_snapshot=True,
            _custom_tensor_prepare_func=_custom_tensor_prepare_func,
        )
        # PendingSnapshot is responsible for closing `storage` and `event_loop`
        return PendingSnapshot(
            path=path,
            pending_io_work=pending_io_work,
            pg_wrapper=pg_wrapper,
            metadata=metadata,
            storage=storage,
            event_loop=event_loop,
            storage_options=storage_options,
        )

    @classmethod
    def _take_impl(
        cls,
        path: str,
        app_state: AppState,
        replicated: Set[str],
        pg_wrapper: PGWrapper,
        storage: StoragePlugin,
        event_loop: asyncio.AbstractEventLoop,
        is_async_snapshot: bool,
        _custom_tensor_prepare_func: Optional[
            Callable[[str, torch.Tensor, bool], torch.Tensor]
        ] = None,
    ) -> Tuple[PendingIOWork, SnapshotMetadata]:
        app_state = app_state.copy()
        rng_state_item = cls._pop_rng_state(app_state=app_state)
        rng_state_dict = None

        manifest: Manifest = {}
        flattened: Dict[str, Any] = {}

        # Invariant: for the same snapshot, the RNG state is the same after
        # .take() and .restore().
        # This can be achieved by ensuring .take() has no side effect on the
        # RNG state. Since we can't guarantee that the .state_dict() method on
        # stateful objects has no side effect on the RNG state, we retrieve the
        # RNG state before saving other stateful objects, and restore the RNG
        # state after saving other stateful objects.
        if rng_state_item is not None:
            key, stateful = rng_state_item
            rng_state_dict = stateful.state_dict()
            mnfst, fltnd = flatten(rng_state_dict, prefix=key)
            manifest.update(mnfst)
            flattened.update(fltnd)

        # Different ranks can register different sets of stateful objects,
        # whose .state_dict() methods may invoke collectives. To avoid
        # potential interleaving of different collectives, we first gather the
        # global key list, then invoke .state_dict() on stateful objects in
        # order with synchronization.
        # TODO: merge this with coalesce path to save an all_gather call
        global_keys = cls._gather_keys(
            keys=list(app_state.keys()), pg_wrapper=pg_wrapper
        )

        for key in global_keys:
            if key in app_state:
                state_dict = app_state[key].state_dict()
                mnfst, fltnd = flatten(state_dict, prefix=key)
                manifest.update(mnfst)
                flattened.update(fltnd)
            pg_wrapper.barrier()

        # Undo any potential side effects to the RNG state. The rest of this
        # function won't affect the RNG state or execute application code.
        if rng_state_item is not None:
            _, stateful = rng_state_item
            stateful.load_state_dict(cast(Dict[str, torch.Tensor], rng_state_dict))

        replicated_paths = cls._calculate_replicated_entries(
            flattened, replicated, pg_wrapper
        )

        object_entries: Dict[str, Entry] = {}
        logical_path_to_write_reqs: Dict[str, List[WriteReq]] = {}
        primitive_entries: Dict[str, PrimitiveEntry] = {}

        for logical_path, obj in flattened.items():
            entry, wrs = prepare_write(
                obj=flattened[logical_path],
                logical_path=logical_path,
                rank=pg_wrapper.get_rank(),
                replicated=logical_path in replicated_paths,
                is_async_snapshot=is_async_snapshot,
                _tensor_prepare_func=functools.partial(
                    _custom_tensor_prepare_func, logical_path
                )
                if _custom_tensor_prepare_func is not None
                else None,
            )
            # Primitive entries don't have write requests
            # and don't need to be partitioned
            if isinstance(entry, PrimitiveEntry):
                primitive_entries[logical_path] = entry
            else:
                object_entries[logical_path] = entry
                logical_path_to_write_reqs[logical_path] = wrs

        object_entries, logical_path_to_write_reqs = partition_write_reqs(
            entries=object_entries, write_reqs=logical_path_to_write_reqs, pg=pg_wrapper
        )
        write_reqs: List[WriteReq] = [
            wr for wrs in logical_path_to_write_reqs.values() for wr in wrs
        ]

        if not is_batching_disabled():
            _, write_reqs = batch_write_requests(
                entries=list(object_entries.values()), write_reqs=write_reqs
            )

        all_entries = dict(**primitive_entries, **object_entries)

        manifest.update(all_entries)
        manifest = cls._gather_manifest(manifest=manifest, pg=pg_wrapper)

        memory_budget_bytes = get_process_memory_budget_bytes(pg=pg_wrapper)
        pending_io_work = sync_execute_write_reqs(
            write_reqs=write_reqs,
            storage=storage,
            memory_budget_bytes=memory_budget_bytes,
            rank=pg_wrapper.get_rank(),
            event_loop=event_loop,
        )
        metadata = SnapshotMetadata(
            version=torchsnapshot_version,
            world_size=pg_wrapper.get_world_size(),
            manifest=manifest,
        )
        return pending_io_work, metadata

    def restore(self, app_state: AppState) -> None:
        """
        Restores the program state from the snapshot.

        Args:
            app_state: The program state to restore from the snapshot.

        """
        torch._C._log_api_usage_once("torchsnapshot.Snapshot.restore")
        self._validate_app_state(app_state)

        event_loop = asyncio.new_event_loop()
        pg_wrapper = PGWrapper(self.pg)
        storage = url_to_storage_plugin_in_event_loop(
            url_path=self.path,
            event_loop=event_loop,
            storage_options=self._storage_options,
        )

        app_state = app_state.copy()
        rng_state_item = self._pop_rng_state(app_state=app_state)

        global_keys = self._gather_keys(
            keys=list(app_state.keys()), pg_wrapper=pg_wrapper
        )
        for key in global_keys:
            self._load_stateful(
                stateful_key=key,
                stateful=app_state.get(key),
                storage=storage,
                pg=pg_wrapper,
                event_loop=event_loop,
            )
            pg_wrapper.barrier()

        # Restore the RNG state last to avoid potential side effects.
        if rng_state_item is not None:
            key, stateful = rng_state_item
            self._load_stateful(
                stateful_key=key,
                stateful=stateful,
                storage=storage,
                pg=pg_wrapper,
                event_loop=event_loop,
            )
        storage.sync_close(event_loop=event_loop)
        event_loop.close()

    @property
    def metadata(self) -> SnapshotMetadata:
        if self._metadata is None:
            event_loop = asyncio.new_event_loop()
            storage = url_to_storage_plugin_in_event_loop(
                url_path=self.path,
                event_loop=event_loop,
                storage_options=self._storage_options,
            )
            self._metadata = self._read_snapshot_metadata(
                storage=storage, event_loop=event_loop
            )
            storage.sync_close(event_loop=event_loop)
            event_loop.close()
        return cast(SnapshotMetadata, self._metadata)

    def read_object(
        self,
        path: str,
        obj_out: Optional[T] = None,
        memory_budget_bytes: Optional[int] = None,
    ) -> T:
        """
        Read a persisted object from the snapshot's content.

        The persisted object to read is specified by its path in the snapshot
        metadata. Available paths can be obtained via `snapshot.get_manifest()`.

        A path in snapshot metadata follows the following format:

            ``RANK/STATEFUL_NAME/STATE_DICT_KEY[/NESTED_CONTAINER_KEY...]``

        The rank only matters when the persisted object is "per-rank".
        Arbitrary rank can be used when the persisted object is "replicated" or
        "sharded".

        If the persisted object is a sharded tensor, `obj_out` must be
        supplied. The supplied tensor can be either a tensor or sharded tensor.
        `read_object` will correctly populate `obj_out`'s data according to
        sharding spec.

        Args:
            path: The path to the persisted object.
            obj_out: If specified and the object type supports in-place load,
                `read_object` will directly read the persisted object into
                `obj_out`'s buffer.
            memory_budget_bytes: When specified, the read operation will keep
                the temporary memory buffer size below this threshold.

        Returns:
            The object read from the snapshot's content.
        """
        torch._C._log_api_usage_once("torchsnapshot.Snapshot.read_object")
        # TODO: better message for malformatted path
        rank_str, unranked_path = path.split("/", 1)
        rank = int(rank_str)
        # Transform the manifest such that (1) replicated entries are made
        # available to the rank (2) sharded tensor shards saved by all ranks
        # are made available to the rank. The availability of the entries is
        # determined from the perspective of the rank specified in the path.
        manifest, merged_sd_entries = get_manifest_for_rank(
            metadata=self.metadata, rank=rank
        )

        if unranked_path not in merged_sd_entries and unranked_path not in manifest:
            # TODO: show candidates based on edit distance
            raise RuntimeError(
                f'The supplied path "{path}" does not exist in the snapshot\'s manifest. '
                "Please verify the available paths within the snapshot via `snapshot.get_manifest()`."
            )
        if not isinstance(obj_out, (torch.Tensor, ShardedTensor)):
            logger.warning(
                f"`obj_out` is of type {type(obj_out)}, which does not support in-place load. "
                "Its state won't be changed after load. The loaded object will be returned."
            )

        event_loop = asyncio.new_event_loop()
        pg_wrapper = PGWrapper(self.pg)
        storage = url_to_storage_plugin_in_event_loop(
            url_path=self.path,
            event_loop=event_loop,
            storage_options=self._storage_options,
        )
        entry = merged_sd_entries.get(unranked_path) or manifest[unranked_path]
        if isinstance(entry, PrimitiveEntry):
            return cast(T, entry.get_value())
        read_reqs, fut = prepare_read(
            entry=entry,
            obj_out=obj_out,
            # TODO: find a suitable buffer_size_limit_bytes to enable chunked
            # read even when memory_budget_bytes is not specified, as chunked
            # tensor read allows for pipelining HtoD copy and storage I/O when
            # reading a single tensor.
            buffer_size_limit_bytes=memory_budget_bytes,
        )

        if not is_batching_disabled():
            read_reqs = batch_read_requests(read_reqs=read_reqs)

        sync_execute_read_reqs(
            read_reqs=read_reqs,
            storage=storage,
            memory_budget_bytes=memory_budget_bytes
            or _MAX_PER_RANK_MEMORY_BUDGET_BYTES,
            rank=pg_wrapper.get_rank(),
            event_loop=event_loop,
        )
        storage.sync_close(event_loop=event_loop)
        event_loop.close()
        return fut.obj

    def get_manifest(self) -> Dict[str, Entry]:
        """
        Returns the snapshot's manifest.

        Returns:
            The snapshot's manifest.
        """
        return copy.deepcopy(self.metadata.manifest)

    @staticmethod
    def _calculate_replicated_entries(
        flattened: Dict[str, Any], replicated: Set[str], pg: PGWrapper
    ) -> Set[str]:
        rank = pg.get_rank()
        world_size = pg.get_world_size()
        replicated_paths = []
        for path, val in flattened.items():
            if any(fnmatch.fnmatch(path, p) for p in replicated) and not isinstance(
                val, ShardedTensor
            ):
                replicated_paths.append(path)
        # pyre-ignore
        obj_list: List[List[str]] = [None] * world_size
        pg.all_gather_object(obj_list, replicated_paths)

        if rank == 0:
            # A path is only treated as replicated if:
            # (1) The path matches one of the patterns specified in `replicated`
            # (2) The path exists on all ranks
            # (3) The value is not sharded
            path_count = defaultdict(int)
            for paths in obj_list:
                for path in paths:
                    path_count[path] += 1
            replicated_paths = list(
                filter(lambda p: path_count[p] == world_size, replicated_paths)
            )
            replicated_paths_list = [replicated_paths]
        else:
            replicated_paths_list = [[]]
        pg.broadcast_object_list(replicated_paths_list, src=0)
        replicated_paths = replicated_paths_list[0]
        return set(replicated_paths)

    @staticmethod
    def _validate_app_state(app_state: AppState) -> None:
        # performs runtime typechecking that all values are Stateful
        for key, value in app_state.items():
            if not isinstance(value, Stateful):
                value_type = type(value)
                raise TypeError(
                    f"Expected Stateful in app_state for key {key}, got {value_type}."
                )

    def _load_stateful(  # noqa
        self,
        stateful_key: str,
        stateful: Optional[Stateful],
        storage: StoragePlugin,
        pg: PGWrapper,
        event_loop: asyncio.AbstractEventLoop,
    ) -> None:
        if stateful is None:
            return

        manifest, merged_sd_entries = get_manifest_for_rank(
            metadata=self.metadata, rank=pg.get_rank()
        )

        # In most cases (e.g. when the stateful is an nn.Module), the stateful
        # has already allocated memory for its tensors. Materializing the
        # persisted state dict and invoking .load_state_dict() would result in
        # a memory footprint that is 2x the size of the stateful. We can reduce
        # the memory footprint by exploiting the fact that most .state_dict()
        # implementations return references to the internal tensors. By loading
        # directly into the already allocated tensors and use them to construct
        # a state dict for .load_state_dict(), we can eliminate an extra
        # intermediate copy of the state. Even if the tensors in the state dict
        # are copies of the internal tensors, this approach would not use more
        # memory compared to the baseline.
        _, flattened = flatten(stateful.state_dict(), prefix=stateful_key)
        flattened = {
            k: v
            for k, v in flattened.items()
            # ShardedTensor became a subclass of torch.Tensor since PyTorch
            # 1.13. We can drop the check for ShardedTensor once PyTorch 1.12.1
            # is no longer supported.
            if isinstance(v, (torch.Tensor, ShardedTensor))
        }

        handle_sharded_tensor_elasticity(
            manifest=manifest,
            merged_sd_entries=merged_sd_entries,
            tensor_requests=list(flattened.keys()),
        )

        container_entries = {}
        read_reqs: List[ReadReq] = []
        futs = {}
        for logical_path, entry in manifest.items():
            if is_container_entry(entry):
                container_entries[logical_path] = entry
                continue

            rrs, fut = prepare_read(
                entry=entry,
                obj_out=flattened.get(logical_path),
            )
            read_reqs += rrs
            futs[logical_path] = fut

            # Free memory in case the items is a copy
            if logical_path in flattened:
                del flattened[logical_path]

        if not is_batching_disabled():
            read_reqs = batch_read_requests(read_reqs=read_reqs)

        memory_budget_bytes = get_process_memory_budget_bytes(pg=pg)
        sync_execute_read_reqs(
            read_reqs=read_reqs,
            storage=storage,
            memory_budget_bytes=memory_budget_bytes,
            rank=pg.get_rank(),
            event_loop=event_loop,
        )

        # Build the originally saved state dict and use it to restore the stateful
        state_dict = inflate(
            manifest=container_entries,
            flattened={k: fut.obj for k, fut in futs.items()},
            prefix=stateful_key,
        )
        stateful.load_state_dict(state_dict)

    @staticmethod
    def _write_snapshot_metadata(
        snapshot_metadata: SnapshotMetadata,
        storage: StoragePlugin,
        event_loop: asyncio.AbstractEventLoop,
    ) -> None:
        write_io = WriteIO(
            path=SNAPSHOT_METADATA_FNAME,
            buf=snapshot_metadata.to_yaml().encode("utf-8"),
        )
        storage.sync_write(write_io=write_io, event_loop=event_loop)

    @staticmethod
    def _read_snapshot_metadata(
        storage: StoragePlugin, event_loop: asyncio.AbstractEventLoop
    ) -> SnapshotMetadata:
        read_io = ReadIO(path=SNAPSHOT_METADATA_FNAME)
        storage.sync_read(read_io=read_io, event_loop=event_loop)
        yaml_str = read_io.buf.getvalue().decode("utf-8")
        return SnapshotMetadata.from_yaml(yaml_str)

    @classmethod
    def _coalesce_path_and_replicated(
        cls,
        path: str,
        pg_wrapper: PGWrapper,
        app_state: AppState,
        replicated: List[str],
    ) -> Tuple[str, Set[str]]:

        rank = pg_wrapper.get_rank()

        # coalesce path
        # TODO: use a single all_gather for both path and replicated.
        # Only emit a single message for path inconsistency.
        obj_list = [path]
        pg_wrapper.broadcast_object_list(obj_list, src=0)
        if obj_list[0] != path:
            logger.warning(
                f"Rank {rank} specified a path ({path}) "
                f"different from rank 0 ({obj_list[0]}). Using path specified by rank 0."
            )

        # TODO: this should be folded into _calculate_replicated_entries
        # coalesce replicated
        replicated = cls._infer_replicated(replicated, app_state)
        # pyre-ignore[9]
        global_replicated: List[List[str]] = [None] * pg_wrapper.get_world_size()
        pg_wrapper.all_gather_object(global_replicated, replicated)

        coalesced_replicated = cls._coalesce_replicated(
            global_replicated=global_replicated
        )
        if set(replicated) != coalesced_replicated:
            logger.warning(
                f"Rank {rank} specified replicated paths: {set(global_replicated[rank])} "
                f"different from replicated paths verified across all ranks: {set(replicated)}"
            )
        return obj_list[0], coalesced_replicated

    @staticmethod
    def _infer_replicated(replicated: List[str], app_state: AppState) -> List[str]:
        new_replicated = replicated.copy()
        if "**" in new_replicated:
            return new_replicated
        for key, val in app_state.items():
            if isinstance(val, DDP):
                ignored = set(cast(List[str], val.parameters_to_ignore))
                if not ignored:
                    new_replicated.append(os.path.join(key, "**"))
                    continue
                for name, _ in itertools.chain(
                    val.named_parameters(), val.named_buffers()
                ):
                    if name not in ignored:
                        new_replicated.append(os.path.join(key, name))
        return new_replicated

    @staticmethod
    def _coalesce_replicated(global_replicated: List[List[str]]) -> Set[str]:
        verified_replicated = set.intersection(*map(set, global_replicated))
        return verified_replicated

    @staticmethod
    def _gather_keys(keys: List[str], pg_wrapper: PGWrapper) -> List[str]:
        # pyre-ignore
        gathered_keys: List[List[str]] = [None] * pg_wrapper.get_world_size()
        pg_wrapper.all_gather_object(gathered_keys, keys)
        return sorted(set(itertools.chain.from_iterable(gathered_keys)))

    @staticmethod
    def _pop_rng_state(
        app_state: AppState,
    ) -> Optional[Tuple[str, RNGState]]:
        rng_state_items = {
            key: stateful
            for key, stateful in app_state.items()
            if isinstance(stateful, RNGState)
        }
        if len(rng_state_items) > 1:
            raise RuntimeError(
                "Multiple RNGState objects in app state: "
                f"{list(rng_state_items.keys())}"
            )
        elif len(rng_state_items) == 1:
            key, stateful = list(rng_state_items.items())[0]
            del app_state[key]
            return key, stateful
        else:
            return None

    @staticmethod
    def _gather_manifest(manifest: Dict[str, Entry], pg: PGWrapper) -> Dict[str, Any]:
        # pyre-ignore
        manifests: List[Dict[str, Entry]] = [None] * pg.get_world_size()
        pg.all_gather_object(manifests, manifest)
        manifests = consolidate_replicated_entries(rank_to_entries=manifests)

        global_manifest = {}
        for rank, manifest in enumerate(manifests):
            for logical_path, entry in manifest.items():
                global_manifest[os.path.join(str(rank), logical_path)] = entry
        return global_manifest


class PendingSnapshot:
    DEFAULT_BARRIER_TIMEOUT = timedelta(seconds=1800)

    def __init__(
        self,
        path: str,
        pending_io_work: PendingIOWork,
        pg_wrapper: PGWrapper,
        metadata: SnapshotMetadata,
        storage: StoragePlugin,
        event_loop: asyncio.AbstractEventLoop,
        storage_options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.path = path
        self.pg: Optional[dist.ProcessGroup] = pg_wrapper.pg
        # pyre-ignore
        self.exc_info: Optional[Any] = None
        self._done = False
        self._storage_options = storage_options

        self.thread = Thread(
            target=self._complete_snapshot,
            kwargs={
                "path": path,
                "rank": pg_wrapper.get_rank(),
                "world_size": pg_wrapper.get_world_size(),
                "pending_io_work": pending_io_work,
                "metadata": metadata,
                "storage": storage,
                "event_loop": event_loop,
                "store": get_or_create_store(pg_wrapper=pg_wrapper),
            },
        )
        self.thread.start()

    def _complete_snapshot(
        self,
        path: str,
        rank: int,
        world_size: int,
        pending_io_work: PendingIOWork,
        metadata: SnapshotMetadata,
        storage: StoragePlugin,
        event_loop: asyncio.AbstractEventLoop,
        store: dist.TCPStore,
    ) -> None:
        # WARNING: do not use any collectives in this method

        # Use a dist.Store-based barrier for synchronization so that the
        # snapshot can be committed in the background thread.
        barrier = LinearBarrier(
            prefix=f"torchsnapshot_{path}",
            store=store,
            rank=rank,
            world_size=world_size,
            leader_rank=0,
        )
        try:
            pending_io_work.sync_complete(event_loop)
            barrier.arrive(timeout=self.DEFAULT_BARRIER_TIMEOUT)

            if rank == 0:
                Snapshot._write_snapshot_metadata(
                    snapshot_metadata=metadata,
                    storage=storage,
                    event_loop=event_loop,
                )
            barrier.depart(timeout=self.DEFAULT_BARRIER_TIMEOUT)
        except Exception as e:
            barrier.report_error(str(e))
            self.exc_info = sys.exc_info()
            logger.warning(
                f"Encountered exception while taking snapshot asynchronously:\n{e}"
            )
        finally:
            storage.sync_close(event_loop=event_loop)
            event_loop.close()
        self._done = True

    def wait(self) -> Snapshot:
        self.thread.join()
        if self.exc_info is not None:
            formatted = "".join(traceback.format_exception(*self.exc_info))
            raise RuntimeError(
                f"Encountered exception while taking snapshot asynchronously:\n{formatted}"
            )
        return Snapshot(
            path=self.path, pg=self.pg, storage_options=self._storage_options
        )

    def done(self) -> bool:
        return self._done

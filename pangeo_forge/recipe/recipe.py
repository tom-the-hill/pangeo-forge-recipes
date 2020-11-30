"""
A Pangeo Forge Recipe
"""

from dataclasses import dataclass

import xarray as xr
import fsspec
from ..utils import chunked_iterable
from .target import Target

from typing import Optional, Iterable, Callable


### How to manually execute a recipe: ###
#
#   t = PangeoForgeTarget()
#   r = MyRecipe(target=t, **opts) # 1
#   # manual execution of recipe
#   r.prepare() # 3
#   for input_key in r.iter_inputs():
#       r.cache_input(input_key) # 4
#   for chunk_key in r.iter_chunks():
#       r.store_chunk(chunk_key) # 5
#   r.finalize() # 6
#
###


# 1) Initialize the Recipe object
# 2) Point the Recipe at its Target
# 3) Initialize the recipe.
#    Check if the target exists; if not, create it.
# 4) cache the inputs to proximate storage (OPTIONAL)
#    Some recipes won't need this (e.g. cloud to cloud)
#    If so, iter_inputs is just an empty iterator
# 5) Load each chunk from the inputs and store it in the target
#    Might be coming from the cache or might be read directly.
# 6)


@dataclass
class DatasetRecipe():
    target: Target

    def prepare(self):
        pass

    def iter_inputs(self):
        return []

    def cache_input(self, input_key):
        raise NotImplementedError

    def iter_chunks(self):
        raise NotImplementedError

    def store_chunk(self, chunk_key):
        raise NotImplementedError

    def finalize(self):
        raise NotImplementedError


@dataclass
class FSSpecFileOpenerMixin:
    input_open_kwargs: dict = {}

    @contextmanager
    def input_opener(self, fname):
        with fsspec.open(fname, **input_open_kwargs) as f:
            yield f


@dataclass
class InputCachingMixin(FSSpecFileOpenerMixin):
    input_cache: InputCache
    require_cache: bool -> False

    # returns a function that takes one input, the input_key
    # this allows us to parallelize these operations
    @property
    def cache_input(self):

        def cache_func(fname: str) -> None:
            with super().input_opener(fname, mode="rb") as source:
                with self.input_cache.open(fname, mode="wb") as target:
                    target.write(source.read())

        return cache_func

    @contextmanager
    def input_opener(self, fname):
        if self.input_cache.exists(fname):
            with self.input_chache.open(fname, mode='rb') as f:
                yield f
        elif self.require_cache:
            raise IOError("Input can only be opened from cache. Call .cache_input first.")
        else:
            # This will bypass the cache. May be slow.
            with super().input_opener(fname, mode="rb") as f:
                yield f



@dataclass
class XarrayInputOpener:
    xarray_open_kwargs: Any

    def open_input(self, fname):
        with self.input_opener(fname)  as f:
            ds = xr.open_dataset(f, **self.xarray_open_kwargs)
        return ds


@dataclass
class XarrayConcatChunkOpener(XarrayInputOpener):
    xarray_concat_kwargs: Any

    def open_chunk(self, chunk_key):
        inputs = self.inputs_for_chunk(chunk_key)
        dsets = [self.open_input(i) for i in inputs]
        combined = xr.concat(dsets, **xarray_concat_kwargs)
        # TODO: maybe do some chunking here?
        return combined


class ZarrWriterMixin:

    @property
    def store_chunk(self) -> Callable:

        def _store_chunk(chunk_key):
            ds_chunk = self.open_chunk(chunk_key)
            target_mapper = self.target.get_mapper()
            write_region = self.get_write_region(chunk_key)
            ds_chunk.to_zarr(target_mapper, region=write_region)

        return _store_chunk


    @property
    def open_target(self) -> Callable:

        def _open_target():
            target_mapper = self.target.get_mapper()
            return xr.open_zarr(target_mapper)

        return _open_target




@dataclass
class FileSequenceRecipe(DatasetRecipe):
    file_urls: Iterable[str]
    sequence_dim: str
    files_per_chunk: int = 1
    nitems_per_file: int = 1


    def __post_init__(self):
        self._chunks_files = {k: v for k, v in
                              enumerate(chunked_iterable(self.file_urls, self.files_per_chunk))}


    def inputs_for_chunk(self, chunk_key):
        return self._chunks_files[chunk_key]


    def nitems_for_chunk(self, chunk_key):
        return self.nitems_per_file * len(self.filenames_for_chunk(chunk_key))


    def region_for_chunk(self, chunk_key):
        # return a dict suitable to pass to xr.to_zarr(region=...)
        # specifies where in the overall array to put this chunk's data
        stride = self.nitems_per_file * self.files_per_chunk
        start = chunk_key * stride
        return {
            self.sequence_dim:
                slice(start, start + self.nitems_for_chunk(chunk_key))
        }


    def sequence_dim(self):
        # tells the total size of dataset along the sequence dimension
        return {
            self.sequence_dim:
                sum([self.nitems_for_chunk(k) for k in self.iter_chunks()])
        }


    def sequence_chunks(self):
        # chunking
        return {self.sequence_dim: self.files_per_chunk * self.nitems_per_file}


    def iter_chunks(self):
        for k in self._chunks_files:
            yield k

    def prepare(self):

        target_store = self.get_store_target()

        try:
            ds = self.open_target(target_store)

        except IOError:
            first_chunk_key = next(self.iter_chunks())
            ds = self.open_chunk(first_chunk_key).chunk()
            ds.to_zarr(path, compute=False, consolidated=False)

        encoding = {v: ds[v].encoding for v in ds}

        # now resize the sequence dim at the zarr level
        sequence_axes = {v: ds[v].get_axis_num(self.sequence_dim)
                         for v in ds
                         if self.sequence_dim in ds[v].dims}
        N = self.sequence_dim()

        zgroup = zarr.open_group(target_store)

        for v, axis in sequence_axes.items():
            arr = zgroup[v]
            shape = list(arr.shape)
            shape[axis] = N
            arr.resize(shape)


class StandardSequentialRecipe(
        FileSequenceRecipe,
        FSSpecInputOpenerMixin,
        XarrayChunkOpenerMixin,
        ZarrWriterMixin,
    ):
    pass

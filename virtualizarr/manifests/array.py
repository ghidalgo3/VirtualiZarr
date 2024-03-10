import re
from typing import Any, Callable, Dict, List, Tuple

import numpy as np

from ..kerchunk import KerchunkArrRefs
from ..zarr import Codec, ZArray
from .manifest import _CHUNK_KEY, ChunkManifest, concat_manifests, stack_manifests

HANDLED_ARRAY_FUNCTIONS: Dict[
    str, Callable
] = {}  # populated by the @implements decorators below


class ManifestArray:
    """
    Virtualized array representation of the chunk data in a single Zarr Array.

    Supports concatenation / stacking, but only if the two arrays to be concatenated have the same codecs.

    Cannot be directly altered.

    Implements subset of the array API standard such that it can be wrapped by xarray.
    Doesn't store the zarr array name, zattrs or ARRAY_DIMENSIONS, as instead those can be stored on a wrapping xarray object.
    """

    # TODO how do we forbid variable-length chunks?

    _manifest: ChunkManifest
    _zarray: ZArray

    def __init__(self, zarray: ZArray, chunkmanifest: ChunkManifest) -> None:
        self._manifest = chunkmanifest
        self._zarray = zarray

    @classmethod
    def from_kerchunk_refs(cls, arr_refs: KerchunkArrRefs) -> "ManifestArray":
        from virtualizarr.kerchunk import fully_decode_arr_refs

        decoded_arr_refs = fully_decode_arr_refs(arr_refs)

        zarray = ZArray.from_kerchunk_refs(decoded_arr_refs[".zarray"])

        kerchunk_chunk_dict = {
            k: v for k, v in decoded_arr_refs.items() if re.match(_CHUNK_KEY, k)
        }
        chunkmanifest = ChunkManifest.from_kerchunk_chunk_dict(kerchunk_chunk_dict)

        obj = object.__new__(cls)
        obj._manifest = chunkmanifest
        obj._zarray = zarray

        return obj

    @property
    def manifest(self) -> ChunkManifest:
        return self._manifest

    @property
    def zarray(self) -> ZArray:
        return self._zarray

    @property
    def chunks(self) -> tuple[int]:
        # TODO do we even need this? The way I implemented concat below I don't think we really do...
        return tuple(self.zarray.chunks)

    @property
    def dtype(self) -> np.dtype:
        dtype_str = self.zarray.dtype
        return np.dtype(dtype_str)

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(int(length) for length in list(self.zarray.shape))

    @property
    def ndim(self) -> int:
        return len(self.shape)

    @property
    def size(self) -> int:
        return np.prod(self.shape)

    @property
    def T(self) -> "ManifestArray":
        raise NotImplementedError()

    def __repr__(self) -> str:
        return f"ManifestArray<shape={self.shape}, dtype={self.dtype}, chunks={self.chunks}>"

    def to_kerchunk_refs(self) -> KerchunkArrRefs:
        # TODO is there enough information to get the attrs and so on here?
        ...

    def to_zarr(self, store) -> None:
        raise NotImplementedError(
            "Requires the chunk manifest ZEP to be formalized before we know what to write out here."
        )

    def __array_function__(self, func, types, args, kwargs) -> Any:
        """
        Hook to teach this class what to do if np.concat etc. is called on it.

        Use this instead of __array_namespace__ so that we don't make promises we can't keep.
        """

        if func not in HANDLED_ARRAY_FUNCTIONS:
            return NotImplemented

        # Note: this allows subclasses that don't override
        # __array_function__ to handle ManifestArray objects
        if not all(issubclass(t, ManifestArray) for t in types):
            return NotImplemented

        return HANDLED_ARRAY_FUNCTIONS[func](*args, **kwargs)

    def __array_ufunc__(self, ufunc, method, *inputs, **kwargs) -> Any:
        """We have to define this in order to convince xarray that this class is a duckarray, even though we will never support ufuncs."""
        return NotImplemented

    def __array__(self) -> np.ndarray:
        raise NotImplementedError(
            "ManifestArrays can't be converted into numpy arrays or pandas Index objects"
        )


def implements(numpy_function):
    """Register an __array_function__ implementation for MyArray objects."""

    def decorator(func):
        HANDLED_ARRAY_FUNCTIONS[numpy_function] = func
        return func

    return decorator


@implements(np.concatenate)
def concatenate(
    arrays: tuple[ManifestArray, ...] | list[ManifestArray], /, *, axis: int | None = 0
) -> ManifestArray:
    """
    Concatenate ManifestArrays by merging their chunk manifests.

    The signature of this function is array API compliant, so that it can be called by `xarray.concat`.
    """
    if axis is None:
        raise NotImplementedError(
            "If axis=None the array API requires flattening, which is a reshape, which can't be implemented on a ManifestArray."
        )

    # TODO make sure it handles axis being negative

    _check_same_dtypes([arr.dtype for arr in arrays])

    shapes = [arr.shape for arr in arrays]
    _check_same_shapes_except_on_concat_axis(shapes, axis)

    # Can't combine different codecs in one manifest
    # see https://github.com/zarr-developers/zarr-specs/issues/288
    _check_same_codecs([arr.zarray.codec for arr in arrays])

    # find what new shape must be
    new_length_along_concat_axis = sum([shape[axis] for shape in shapes])
    first_shape, *_ = shapes
    new_shape = list(first_shape)
    new_shape[axis] = new_length_along_concat_axis

    concatenated_manifest = concat_manifests(
        [arr._manifest for arr in arrays],
        axis=axis,
    )
    new_shape = ...
    new_zarray = _replace_shape(arrays[0]._zarray, new_shape)

    return ManifestArray(chunkmanifest=concatenated_manifest, zarray=new_zarray)


def _check_same_dtypes(dtypes: list[np.dtype]) -> None:
    """Check all the dtypes are the same"""

    first_dtype, *other_dtypes = dtypes
    for other_dtype in other_dtypes:
        if other_dtype != first_dtype:
            raise ValueError(
                f"Cannot concatenate arrays with inconsistent dtypes: {other_dtype} vs {first_dtype}"
            )


def _check_same_shapes_except_on_concat_axis(shapes: list[tuple[int, ...]], axis: int):
    """Check that shapes are compatible for concatenation"""
    shapes_without_concat_axis = [
        _remove_element_at_position(shape, axis) for shape in shapes
    ]

    first_shape, *other_shapes = shapes_without_concat_axis
    for other_shape in other_shapes:
        if other_shape != first_shape:
            raise ValueError(
                f"Cannot concatenate arrays with shapes {[shape for shape in shapes]}"
            )


def _remove_element_at_position(t: tuple[Any], pos: int) -> tuple[Any]:
    new_l = list(t)
    new_l.pop(pos)
    return tuple(new_l)


def _check_same_codecs(codecs: List[Codec]) -> None:
    first_codec, *other_codecs = codecs
    for codec in other_codecs:
        if codec != first_codec:
            raise NotImplementedError(
                "The ManifestArray class cannot concatenate arrays which were stored using different codecs, "
                f"But found codecs {first_codec} vs {codec} ."
                "See https://github.com/zarr-developers/zarr-specs/issues/288"
            )


def _replace_shape(zarray: ZArray, new_shape: Tuple[int, ...]) -> ZArray:
    ...


@implements(np.result_type)
def result_type(*arrays_and_dtypes) -> np.dtype:
    """Called by xarray to ensure all arguments to concat have the same dtype."""
    first_dtype, *other_dtypes = [np.dtype(obj) for obj in arrays_and_dtypes]
    for other_dtype in other_dtypes:
        if other_dtype != first_dtype:
            raise ValueError("dtypes not all consistent")
    return first_dtype
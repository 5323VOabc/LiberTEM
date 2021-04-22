import pytest
import numpy as np
from unittest import mock
import copy

from libertem.executor.inline import InlineJobExecutor
from libertem.io.dataset.base import MMapBackend
from libertem.udf.sum import SumUDF
from libertem.udf.sumsigudf import SumSigUDF
from libertem.api import Context


def test_ctx_load(lt_ctx, default_raw):
    lt_ctx.load(
        "raw",
        path=default_raw._path,
        nav_shape=(16, 16),
        dtype="float32",
        sig_shape=(128, 128),
    )


def test_context_arguments():
    with pytest.raises(ValueError):
        # refs https://github.com/LiberTEM/LiberTEM/issues/918
        Context(executor=InlineJobExecutor)


def test_run_udf_with_io_backend(lt_ctx, default_raw):
    io_backend = MMapBackend(enable_readahead_hints=True)
    lt_ctx.load(
        "raw",
        path=default_raw._path,
        io_backend=io_backend,
        dtype="float32",
        nav_shape=(16, 16),
        sig_shape=(128, 128),
    )
    res = lt_ctx.run_udf(dataset=default_raw, udf=SumUDF())
    assert np.array(res['intensity']).shape == (128, 128)


def test_multi_udf(lt_ctx, default_raw):
    udfs = [SumUDF(), SumSigUDF()]
    combined_res = lt_ctx.run_udf(dataset=default_raw, udf=udfs)
    ref_res = (
        lt_ctx.run_udf(dataset=default_raw, udf=udfs[0]),
        lt_ctx.run_udf(dataset=default_raw, udf=udfs[1]),
    )

    assert isinstance(ref_res[0], dict)
    for index, res in enumerate(ref_res):
        for key in res.keys():
            assert np.all(res[key].data == combined_res[index][key].data)


@pytest.mark.asyncio
async def test_multi_udf_async(lt_ctx, default_raw):
    udfs = [SumUDF(), SumSigUDF()]
    combined_res = await lt_ctx.run_udf(dataset=default_raw, udf=udfs, sync=False)
    single_res = (
        await lt_ctx.run_udf(dataset=default_raw, udf=udfs[0], sync=False),
        await lt_ctx.run_udf(dataset=default_raw, udf=udfs[1], sync=False),
    )
    ref_res = (
        lt_ctx.run_udf(dataset=default_raw, udf=udfs[0]),
        lt_ctx.run_udf(dataset=default_raw, udf=udfs[1]),
    )
    assert isinstance(ref_res[0], dict)
    for index, res in enumerate(ref_res):
        for key in res.keys():
            assert np.all(res[key].data == combined_res[index][key].data)
            assert np.all(single_res[index][key].data == combined_res[index][key].data)


def test_udf_iter(lt_ctx, default_raw):
    udfs = [SumUDF(), SumSigUDF()]
    for res in lt_ctx.run_udf_iter(dataset=default_raw, udf=udfs):
        ref_res = lt_ctx.run_udf(dataset=default_raw, udf=copy.deepcopy(udfs), roi=res.damage.data)
        # kind='sig'
        for key in ref_res[0].keys():
            ref_item = ref_res[0][key].data
            res_item = res.buffers[0][key].data
            assert np.all(ref_item == res_item)
        # kind='nav'
        for key in ref_res[1].keys():
            ref_item = ref_res[1][key].data[res.damage.data]
            res_item = res.buffers[1][key].data[res.damage.data]
            assert np.all(ref_item == res_item)


@pytest.mark.asyncio
async def test_udf_iter_async(lt_ctx, default_raw):
    udfs = [SumUDF(), SumSigUDF()]
    async for res in lt_ctx.run_udf_iter(dataset=default_raw, udf=udfs, sync=False):
        # Nested execution of UDFs doesn't work, so we take a copy
        ref_res = lt_ctx.run_udf(dataset=default_raw, udf=copy.deepcopy(udfs), roi=res.damage.data)
        # kind='sig'
        for key in ref_res[0].keys():
            ref_item = ref_res[0][key].data
            res_item = res.buffers[0][key].data
            assert np.all(ref_item == res_item)
        # kind='nav'
        for key in ref_res[1].keys():
            ref_item = ref_res[1][key].data[res.damage.data]
            res_item = res.buffers[1][key].data[res.damage.data]
            assert np.all(ref_item == res_item)


@pytest.mark.parametrize(
    'plots', (
        True,
        [['intensity']],
        [[], [('intensity', np.abs), ('intensity', np.sin)]],
        None
    )
)
def test_plots(lt_ctx, default_raw, plots):
    udfs = [SumUDF(), SumSigUDF()]
    if plots is None:
        plots = [lt_ctx.plot_class(dataset=default_raw, udf=udfs[0])]
    with mock.patch.object(lt_ctx.plot_class, 'update') as plot_update:
        with mock.patch.object(lt_ctx.plot_class, 'display') as plot_display:
            combined_res = lt_ctx.run_udf(dataset=default_raw, udf=udfs, plots=True)
            plot_update.assert_called()
            plot_display.assert_called()
    ref_res = (
        lt_ctx.run_udf(dataset=default_raw, udf=udfs[0]),
        lt_ctx.run_udf(dataset=default_raw, udf=udfs[1]),
    )

    assert isinstance(ref_res[0], dict)
    for index, res in enumerate(ref_res):
        for key in res.keys():
            assert np.all(res[key].data == combined_res[index][key].data)


@pytest.mark.parametrize(
    'use_roi', (False, True)
)
def test_display(lt_ctx, default_raw, use_roi):
    if use_roi:
        roi = np.random.choice((True, False), size=default_raw.shape.nav)
    else:
        roi = None
    udf = SumUDF()
    d = lt_ctx.display(dataset=default_raw, udf=udf, roi=roi)
    print(d._repr_html_())

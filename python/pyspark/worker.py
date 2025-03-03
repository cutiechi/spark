#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
Worker that receives input from Piped RDD.
"""
import os
import sys
import time
from inspect import getfullargspec
import json
from typing import Any, Callable, Iterable, Iterator

import traceback
import faulthandler

from pyspark.accumulators import _accumulatorRegistry
from pyspark.java_gateway import local_connect_and_auth
from pyspark.taskcontext import BarrierTaskContext, TaskContext
from pyspark.resource import ResourceInformation
from pyspark.rdd import PythonEvalType
from pyspark.serializers import (
    write_with_length,
    write_int,
    read_long,
    read_bool,
    write_long,
    read_int,
    SpecialLengths,
    UTF8Deserializer,
    CPickleSerializer,
    BatchedSerializer,
)
from pyspark.sql.pandas.serializers import (
    ArrowStreamPandasUDFSerializer,
    ArrowStreamPandasUDTFSerializer,
    CogroupUDFSerializer,
    ArrowStreamUDFSerializer,
    ApplyInPandasWithStateSerializer,
)
from pyspark.sql.pandas.types import to_arrow_type
from pyspark.sql.types import BinaryType, Row, StringType, StructType, _parse_datatype_json_string
from pyspark.util import fail_on_stopiteration, try_simplify_traceback
from pyspark import shuffle
from pyspark.errors import PySparkRuntimeError, PySparkTypeError
from pyspark.worker_util import (
    check_python_version,
    read_command,
    pickleSer,
    send_accumulator_updates,
    setup_broadcasts,
    setup_memory_limits,
    setup_spark_files,
    utf8_deserializer,
)


def report_times(outfile, boot, init, finish):
    write_int(SpecialLengths.TIMING_DATA, outfile)
    write_long(int(1000 * boot), outfile)
    write_long(int(1000 * init), outfile)
    write_long(int(1000 * finish), outfile)


def chain(f, g):
    """chain two functions together"""
    return lambda *a: g(f(*a))


def wrap_udf(f, return_type):
    if return_type.needConversion():
        toInternal = return_type.toInternal
        return lambda *a, **kw: toInternal(f(*a, **kw))
    else:
        return lambda *a, **kw: f(*a, **kw)


def wrap_scalar_pandas_udf(f, return_type):
    arrow_return_type = to_arrow_type(return_type)

    def verify_result_type(result):
        if not hasattr(result, "__len__"):
            pd_type = "pandas.DataFrame" if type(return_type) == StructType else "pandas.Series"
            raise PySparkTypeError(
                error_class="UDF_RETURN_TYPE",
                message_parameters={
                    "expected": pd_type,
                    "actual": type(result).__name__,
                },
            )
        return result

    def verify_result_length(result, length):
        if len(result) != length:
            raise PySparkRuntimeError(
                error_class="SCHEMA_MISMATCH_FOR_PANDAS_UDF",
                message_parameters={
                    "expected": str(length),
                    "actual": str(len(result)),
                },
            )
        return result

    return lambda *a, **kw: (
        verify_result_length(
            verify_result_type(f(*a, **kw)), len((list(a) + list(kw.values()))[0])
        ),
        arrow_return_type,
    )


def wrap_arrow_batch_udf(f, return_type):
    import pandas as pd

    arrow_return_type = to_arrow_type(return_type)

    # "result_func" ensures the result of a Python UDF to be consistent with/without Arrow
    # optimization.
    # Otherwise, an Arrow-optimized Python UDF raises "pyarrow.lib.ArrowTypeError: Expected a
    # string or bytes dtype, got ..." whereas a non-Arrow-optimized Python UDF returns
    # successfully.
    result_func = lambda pdf: pdf  # noqa: E731
    if type(return_type) == StringType:
        result_func = lambda r: str(r) if r is not None else r  # noqa: E731
    elif type(return_type) == BinaryType:
        result_func = lambda r: bytes(r) if r is not None else r  # noqa: E731

    def evaluate(*args: pd.Series, **kwargs: pd.Series) -> pd.Series:
        keys = list(kwargs.keys())
        len_args = len(args)
        return pd.Series(
            [
                result_func(
                    f(*row[:len_args], **{key: row[len_args + i] for i, key in enumerate(keys)})
                )
                for row in zip(*args, *[kwargs[key] for key in keys])
            ]
        )

    def verify_result_type(result):
        if not hasattr(result, "__len__"):
            pd_type = "pandas.DataFrame" if type(return_type) == StructType else "pandas.Series"
            raise PySparkTypeError(
                error_class="UDF_RETURN_TYPE",
                message_parameters={
                    "expected": pd_type,
                    "actual": type(result).__name__,
                },
            )
        return result

    def verify_result_length(result, length):
        if len(result) != length:
            raise PySparkRuntimeError(
                error_class="SCHEMA_MISMATCH_FOR_PANDAS_UDF",
                message_parameters={
                    "expected": str(length),
                    "actual": str(len(result)),
                },
            )
        return result

    return lambda *a, **kw: (
        verify_result_length(
            verify_result_type(evaluate(*a, **kw)), len((list(a) + list(kw.values()))[0])
        ),
        arrow_return_type,
    )


def wrap_pandas_batch_iter_udf(f, return_type):
    arrow_return_type = to_arrow_type(return_type)
    iter_type_label = "pandas.DataFrame" if type(return_type) == StructType else "pandas.Series"

    def verify_result(result):
        if not isinstance(result, Iterator) and not hasattr(result, "__iter__"):
            raise PySparkTypeError(
                error_class="UDF_RETURN_TYPE",
                message_parameters={
                    "expected": "iterator of {}".format(iter_type_label),
                    "actual": type(result).__name__,
                },
            )
        return result

    def verify_element(elem):
        import pandas as pd

        if not isinstance(elem, pd.DataFrame if type(return_type) == StructType else pd.Series):
            raise PySparkTypeError(
                error_class="UDF_RETURN_TYPE",
                message_parameters={
                    "expected": "iterator of {}".format(iter_type_label),
                    "actual": "iterator of {}".format(type(elem).__name__),
                },
            )

        verify_pandas_result(
            elem, return_type, assign_cols_by_name=True, truncate_return_schema=True
        )

        return elem

    return lambda *iterator: map(
        lambda res: (res, arrow_return_type), map(verify_element, verify_result(f(*iterator)))
    )


def verify_pandas_result(result, return_type, assign_cols_by_name, truncate_return_schema):
    import pandas as pd

    if type(return_type) == StructType:
        if not isinstance(result, pd.DataFrame):
            raise PySparkTypeError(
                error_class="UDF_RETURN_TYPE",
                message_parameters={
                    "expected": "pandas.DataFrame",
                    "actual": type(result).__name__,
                },
            )

        # check the schema of the result only if it is not empty or has columns
        if not result.empty or len(result.columns) != 0:
            # if any column name of the result is a string
            # the column names of the result have to match the return type
            #   see create_array in pyspark.sql.pandas.serializers.ArrowStreamPandasSerializer
            field_names = set([field.name for field in return_type.fields])
            # only the first len(field_names) result columns are considered
            # when truncating the return schema
            result_columns = (
                result.columns[: len(field_names)] if truncate_return_schema else result.columns
            )
            column_names = set(result_columns)
            if (
                assign_cols_by_name
                and any(isinstance(name, str) for name in result.columns)
                and column_names != field_names
            ):
                missing = sorted(list(field_names.difference(column_names)))
                missing = f" Missing: {', '.join(missing)}." if missing else ""

                extra = sorted(list(column_names.difference(field_names)))
                extra = f" Unexpected: {', '.join(extra)}." if extra else ""

                raise PySparkRuntimeError(
                    error_class="RESULT_COLUMNS_MISMATCH_FOR_PANDAS_UDF",
                    message_parameters={
                        "missing": missing,
                        "extra": extra,
                    },
                )
            # otherwise the number of columns of result have to match the return type
            elif len(result_columns) != len(return_type):
                raise PySparkRuntimeError(
                    error_class="RESULT_LENGTH_MISMATCH_FOR_PANDAS_UDF",
                    message_parameters={
                        "expected": str(len(return_type)),
                        "actual": str(len(result.columns)),
                    },
                )
    else:
        if not isinstance(result, pd.Series):
            raise PySparkTypeError(
                error_class="UDF_RETURN_TYPE",
                message_parameters={"expected": "pandas.Series", "actual": type(result).__name__},
            )


def wrap_arrow_batch_iter_udf(f, return_type):
    arrow_return_type = to_arrow_type(return_type)

    def verify_result(result):
        if not isinstance(result, Iterator) and not hasattr(result, "__iter__"):
            raise PySparkTypeError(
                error_class="UDF_RETURN_TYPE",
                message_parameters={
                    "expected": "iterator of pyarrow.RecordBatch",
                    "actual": type(result).__name__,
                },
            )
        return result

    def verify_element(elem):
        import pyarrow as pa

        if not isinstance(elem, pa.RecordBatch):
            raise PySparkTypeError(
                error_class="UDF_RETURN_TYPE",
                message_parameters={
                    "expected": "iterator of pyarrow.RecordBatch",
                    "actual": "iterator of {}".format(type(elem).__name__),
                },
            )

        return elem

    return lambda *iterator: map(
        lambda res: (res, arrow_return_type), map(verify_element, verify_result(f(*iterator)))
    )


def wrap_cogrouped_map_pandas_udf(f, return_type, argspec, runner_conf):
    _assign_cols_by_name = assign_cols_by_name(runner_conf)

    def wrapped(left_key_series, left_value_series, right_key_series, right_value_series):
        import pandas as pd

        left_df = pd.concat(left_value_series, axis=1)
        right_df = pd.concat(right_value_series, axis=1)

        if len(argspec.args) == 2:
            result = f(left_df, right_df)
        elif len(argspec.args) == 3:
            key_series = left_key_series if not left_df.empty else right_key_series
            key = tuple(s[0] for s in key_series)
            result = f(key, left_df, right_df)
        verify_pandas_result(
            result, return_type, _assign_cols_by_name, truncate_return_schema=False
        )

        return result

    return lambda kl, vl, kr, vr: [(wrapped(kl, vl, kr, vr), to_arrow_type(return_type))]


def wrap_grouped_map_pandas_udf(f, return_type, argspec, runner_conf):
    _assign_cols_by_name = assign_cols_by_name(runner_conf)

    def wrapped(key_series, value_series):
        import pandas as pd

        if len(argspec.args) == 1:
            result = f(pd.concat(value_series, axis=1))
        elif len(argspec.args) == 2:
            key = tuple(s[0] for s in key_series)
            result = f(key, pd.concat(value_series, axis=1))
        verify_pandas_result(
            result, return_type, _assign_cols_by_name, truncate_return_schema=False
        )

        return result

    return lambda k, v: [(wrapped(k, v), to_arrow_type(return_type))]


def wrap_grouped_map_pandas_udf_with_state(f, return_type):
    """
    Provides a new lambda instance wrapping user function of applyInPandasWithState.

    The lambda instance receives (key series, iterator of value series, state) and performs
    some conversion to be adapted with the signature of user function.

    See the function doc of inner function `wrapped` for more details on what adapter does.
    See the function doc of `mapper` function for
    `eval_type == PythonEvalType.SQL_GROUPED_MAP_PANDAS_UDF_WITH_STATE` for more details on
    the input parameters of lambda function.

    Along with the returned iterator, the lambda instance will also produce the return_type as
    converted to the arrow schema.
    """

    def wrapped(key_series, value_series_gen, state):
        """
        Provide an adapter of the user function performing below:

        - Extract the first value of all columns in key series and produce as a tuple.
        - If the state has timed out, call the user function with empty pandas DataFrame.
        - If not, construct a new generator which converts each element of value series to
          pandas DataFrame (lazy evaluation), and call the user function with the generator
        - Verify each element of returned iterator to check the schema of pandas DataFrame.
        """
        import pandas as pd

        key = tuple(s[0] for s in key_series)

        if state.hasTimedOut:
            # Timeout processing pass empty iterator. Here we return an empty DataFrame instead.
            values = [
                pd.DataFrame(columns=pd.concat(next(value_series_gen), axis=1).columns),
            ]
        else:
            values = (pd.concat(x, axis=1) for x in value_series_gen)

        result_iter = f(key, values, state)

        def verify_element(result):
            if not isinstance(result, pd.DataFrame):
                raise PySparkTypeError(
                    error_class="UDF_RETURN_TYPE",
                    message_parameters={
                        "expected": "iterator of pandas.DataFrame",
                        "actual": "iterator of {}".format(type(result).__name__),
                    },
                )
            # the number of columns of result have to match the return type
            # but it is fine for result to have no columns at all if it is empty
            if not (
                len(result.columns) == len(return_type)
                or (len(result.columns) == 0 and result.empty)
            ):
                raise PySparkRuntimeError(
                    error_class="RESULT_LENGTH_MISMATCH_FOR_PANDAS_UDF",
                    message_parameters={
                        "expected": str(len(return_type)),
                        "actual": str(len(result.columns)),
                    },
                )

            return result

        if isinstance(result_iter, pd.DataFrame):
            raise PySparkTypeError(
                error_class="UDF_RETURN_TYPE",
                message_parameters={
                    "expected": "iterable of pandas.DataFrame",
                    "actual": type(result_iter).__name__,
                },
            )

        try:
            iter(result_iter)
        except TypeError:
            raise PySparkTypeError(
                error_class="UDF_RETURN_TYPE",
                message_parameters={"expected": "iterable", "actual": type(result_iter).__name__},
            )

        result_iter_with_validation = (verify_element(x) for x in result_iter)

        return (
            result_iter_with_validation,
            state,
        )

    return lambda k, v, s: [(wrapped(k, v, s), to_arrow_type(return_type))]


def wrap_grouped_agg_pandas_udf(f, return_type):
    arrow_return_type = to_arrow_type(return_type)

    def wrapped(*args, **kwargs):
        import pandas as pd

        result = f(*args, **kwargs)
        return pd.Series([result])

    return lambda *a, **kw: (wrapped(*a, **kw), arrow_return_type)


def wrap_window_agg_pandas_udf(f, return_type, runner_conf, udf_index):
    window_bound_types_str = runner_conf.get("pandas_window_bound_types")
    window_bound_type = [t.strip().lower() for t in window_bound_types_str.split(",")][udf_index]
    if window_bound_type == "bounded":
        return wrap_bounded_window_agg_pandas_udf(f, return_type)
    elif window_bound_type == "unbounded":
        return wrap_unbounded_window_agg_pandas_udf(f, return_type)
    else:
        raise PySparkRuntimeError(
            error_class="INVALID_WINDOW_BOUND_TYPE",
            message_parameters={
                "window_bound_type": window_bound_type,
            },
        )


def wrap_unbounded_window_agg_pandas_udf(f, return_type):
    # This is similar to grouped_agg_pandas_udf, the only difference
    # is that window_agg_pandas_udf needs to repeat the return value
    # to match window length, where grouped_agg_pandas_udf just returns
    # the scalar value.
    arrow_return_type = to_arrow_type(return_type)

    def wrapped(*args, **kwargs):
        import pandas as pd

        result = f(*args, **kwargs)
        return pd.Series([result]).repeat(len((list(args) + list(kwargs.values()))[0]))

    return lambda *a, **kw: (wrapped(*a, **kw), arrow_return_type)


def wrap_bounded_window_agg_pandas_udf(f, return_type):
    arrow_return_type = to_arrow_type(return_type)

    def wrapped(begin_index, end_index, *args, **kwargs):
        import pandas as pd

        result = []

        # Index operation is faster on np.ndarray,
        # So we turn the index series into np array
        # here for performance
        begin_array = begin_index.values
        end_array = end_index.values

        for i in range(len(begin_array)):
            # Note: Create a slice from a series for each window is
            #       actually pretty expensive. However, there
            #       is no easy way to reduce cost here.
            # Note: s.iloc[i : j] is about 30% faster than s[i: j], with
            #       the caveat that the created slices shares the same
            #       memory with s. Therefore, user are not allowed to
            #       change the value of input series inside the window
            #       function. It is rare that user needs to modify the
            #       input series in the window function, and therefore,
            #       it is be a reasonable restriction.
            # Note: Calling reset_index on the slices will increase the cost
            #       of creating slices by about 100%. Therefore, for performance
            #       reasons we don't do it here.
            args_slices = [s.iloc[begin_array[i] : end_array[i]] for s in args]
            kwargs_slices = {k: s.iloc[begin_array[i] : end_array[i]] for k, s in kwargs.items()}
            result.append(f(*args_slices, **kwargs_slices))
        return pd.Series(result)

    return lambda *a, **kw: (wrapped(*a, **kw), arrow_return_type)


def read_single_udf(pickleSer, infile, eval_type, runner_conf, udf_index):
    num_arg = read_int(infile)

    if eval_type in (
        PythonEvalType.SQL_BATCHED_UDF,
        PythonEvalType.SQL_ARROW_BATCHED_UDF,
        PythonEvalType.SQL_SCALAR_PANDAS_UDF,
        PythonEvalType.SQL_GROUPED_AGG_PANDAS_UDF,
        PythonEvalType.SQL_WINDOW_AGG_PANDAS_UDF,
        # The below doesn't support named argument, but shares the same protocol.
        PythonEvalType.SQL_SCALAR_PANDAS_ITER_UDF,
    ):
        args_offsets = []
        kwargs_offsets = {}
        for _ in range(num_arg):
            offset = read_int(infile)
            if read_bool(infile):
                name = utf8_deserializer.loads(infile)
                kwargs_offsets[name] = offset
            else:
                args_offsets.append(offset)
    else:
        args_offsets = [read_int(infile) for i in range(num_arg)]
        kwargs_offsets = {}

    chained_func = None
    for i in range(read_int(infile)):
        f, return_type = read_command(pickleSer, infile)
        if chained_func is None:
            chained_func = f
        else:
            chained_func = chain(chained_func, f)

    if eval_type == PythonEvalType.SQL_SCALAR_PANDAS_ITER_UDF:
        func = chained_func
    else:
        # make sure StopIteration's raised in the user code are not ignored
        # when they are processed in a for loop, raise them as RuntimeError's instead
        func = fail_on_stopiteration(chained_func)

    # the last returnType will be the return type of UDF
    if eval_type == PythonEvalType.SQL_SCALAR_PANDAS_UDF:
        udf = wrap_scalar_pandas_udf(func, return_type)
    elif eval_type == PythonEvalType.SQL_ARROW_BATCHED_UDF:
        udf = wrap_arrow_batch_udf(func, return_type)
    elif eval_type == PythonEvalType.SQL_SCALAR_PANDAS_ITER_UDF:
        udf = wrap_pandas_batch_iter_udf(func, return_type)
    elif eval_type == PythonEvalType.SQL_MAP_PANDAS_ITER_UDF:
        udf = wrap_pandas_batch_iter_udf(func, return_type)
    elif eval_type == PythonEvalType.SQL_MAP_ARROW_ITER_UDF:
        udf = wrap_arrow_batch_iter_udf(func, return_type)
    elif eval_type == PythonEvalType.SQL_GROUPED_MAP_PANDAS_UDF:
        argspec = getfullargspec(chained_func)  # signature was lost when wrapping it
        udf = wrap_grouped_map_pandas_udf(func, return_type, argspec, runner_conf)
    elif eval_type == PythonEvalType.SQL_GROUPED_MAP_PANDAS_UDF_WITH_STATE:
        udf = wrap_grouped_map_pandas_udf_with_state(func, return_type)
    elif eval_type == PythonEvalType.SQL_COGROUPED_MAP_PANDAS_UDF:
        argspec = getfullargspec(chained_func)  # signature was lost when wrapping it
        udf = wrap_cogrouped_map_pandas_udf(func, return_type, argspec, runner_conf)
    elif eval_type == PythonEvalType.SQL_GROUPED_AGG_PANDAS_UDF:
        udf = wrap_grouped_agg_pandas_udf(func, return_type)
    elif eval_type == PythonEvalType.SQL_WINDOW_AGG_PANDAS_UDF:
        udf = wrap_window_agg_pandas_udf(func, return_type, runner_conf, udf_index)
    elif eval_type == PythonEvalType.SQL_BATCHED_UDF:
        udf = wrap_udf(func, return_type)
    else:
        raise ValueError("Unknown eval type: {}".format(eval_type))
    return args_offsets, kwargs_offsets, udf


# Used by SQL_GROUPED_MAP_PANDAS_UDF and SQL_SCALAR_PANDAS_UDF and SQL_ARROW_BATCHED_UDF when
# returning StructType
def assign_cols_by_name(runner_conf):
    return (
        runner_conf.get(
            "spark.sql.legacy.execution.pandas.groupedMap.assignColumnsByName", "true"
        ).lower()
        == "true"
    )


# Read and process a serialized user-defined table function (UDTF) from a socket.
# It expects the UDTF to be in a specific format and performs various checks to
# ensure the UDTF is valid. This function also prepares a mapper function for applying
# the UDTF logic to input rows.
def read_udtf(pickleSer, infile, eval_type):
    if eval_type == PythonEvalType.SQL_ARROW_TABLE_UDF:
        runner_conf = {}
        # Load conf used for arrow evaluation.
        num_conf = read_int(infile)
        for i in range(num_conf):
            k = utf8_deserializer.loads(infile)
            v = utf8_deserializer.loads(infile)
            runner_conf[k] = v

        # NOTE: if timezone is set here, that implies respectSessionTimeZone is True
        timezone = runner_conf.get("spark.sql.session.timeZone", None)
        safecheck = (
            runner_conf.get("spark.sql.execution.pandas.convertToArrowArraySafely", "false").lower()
            == "true"
        )
        ser = ArrowStreamPandasUDTFSerializer(timezone, safecheck)
    else:
        # Each row is a group so do not batch but send one by one.
        ser = BatchedSerializer(CPickleSerializer(), 1)

    # See `PythonUDTFRunner.PythonUDFWriterThread.writeCommand'
    num_arg = read_int(infile)
    args_offsets = []
    kwargs_offsets = {}
    for _ in range(num_arg):
        offset = read_int(infile)
        if read_bool(infile):
            name = utf8_deserializer.loads(infile)
            kwargs_offsets[name] = offset
        else:
            args_offsets.append(offset)
    num_partition_child_indexes = read_int(infile)
    partition_child_indexes = [read_int(infile) for i in range(num_partition_child_indexes)]
    handler = read_command(pickleSer, infile)
    if not isinstance(handler, type):
        raise PySparkRuntimeError(
            f"Invalid UDTF handler type. Expected a class (type 'type'), but "
            f"got an instance of {type(handler).__name__}."
        )

    return_type = _parse_datatype_json_string(utf8_deserializer.loads(infile))
    if not type(return_type) == StructType:
        raise PySparkRuntimeError(
            f"The return type of a UDTF must be a struct type, but got {type(return_type)}."
        )

    class UDTFWithPartitions:
        """
        This implements the logic of a UDTF that accepts an input TABLE argument with one or more
        PARTITION BY expressions.

        For example, let's assume we have a table like:
            CREATE TABLE t (c1 INT, c2 INT) USING delta;
        Then for the following queries:
            SELECT * FROM my_udtf(TABLE (t) PARTITION BY c1, c2);
            The partition_child_indexes will be: 0, 1.
            SELECT * FROM my_udtf(TABLE (t) PARTITION BY c1, c2 + 4);
            The partition_child_indexes will be: 0, 2 (where we add a projection for "c2 + 4").
        """

        def __init__(self, create_udtf: Callable, partition_child_indexes: list):
            """
            Creates a new instance of this class to wrap the provided UDTF with another one that
            checks the values of projected partitioning expressions on consecutive rows to figure
            out when the partition boundaries change.

            Parameters
            ----------
            create_udtf: function
                Function to create a new instance of the UDTF to be invoked.
            partition_child_indexes: list
                List of integers identifying zero-based indexes of the columns of the input table
                that contain projected partitioning expressions. This class will inspect these
                values for each pair of consecutive input rows. When they change, this indicates
                the boundary between two partitions, and we will invoke the 'terminate' method on
                the UDTF class instance and then destroy it and create a new one to implement the
                desired partitioning semantics.
            """
            self._create_udtf: Callable = create_udtf
            self._udtf = create_udtf()
            self._prev_arguments: list = list()
            self._partition_child_indexes: list = partition_child_indexes

        def eval(self, *args, **kwargs) -> Iterator:
            changed_partitions = self._check_partition_boundaries(
                list(args) + list(kwargs.values())
            )
            if changed_partitions:
                if self._udtf.terminate is not None:
                    result = self._udtf.terminate()
                    if result is not None:
                        for row in result:
                            yield row
                self._udtf = self._create_udtf()
            if self._udtf.eval is not None:
                result = self._udtf.eval(*args, **kwargs)
                if result is not None:
                    for row in result:
                        yield row

        def terminate(self) -> Iterator:
            if self._udtf.terminate is not None:
                return self._udtf.terminate()
            return iter(())

        def _check_partition_boundaries(self, arguments: list) -> bool:
            result = False
            if len(self._prev_arguments) > 0:
                cur_table_arg = self._get_table_arg(arguments)
                prev_table_arg = self._get_table_arg(self._prev_arguments)
                cur_partitions_args = []
                prev_partitions_args = []
                for i in partition_child_indexes:
                    cur_partitions_args.append(cur_table_arg[i])
                    prev_partitions_args.append(prev_table_arg[i])
                self._prev_arguments = arguments
                result = any(k != v for k, v in zip(cur_partitions_args, prev_partitions_args))
            self._prev_arguments = arguments
            return result

        def _get_table_arg(self, inputs: list) -> Row:
            return [x for x in inputs if type(x) is Row][0]

    # Instantiate the UDTF class.
    try:
        if len(partition_child_indexes) > 0:
            udtf = UDTFWithPartitions(handler, partition_child_indexes)
        else:
            udtf = handler()
    except Exception as e:
        raise PySparkRuntimeError(
            error_class="UDTF_EXEC_ERROR",
            message_parameters={"method_name": "__init__", "error": str(e)},
        )

    # Validate the UDTF
    if not hasattr(udtf, "eval"):
        raise PySparkRuntimeError(
            "Failed to execute the user defined table function because it has not "
            "implemented the 'eval' method. Please add the 'eval' method and try "
            "the query again."
        )

    if eval_type == PythonEvalType.SQL_ARROW_TABLE_UDF:

        def wrap_arrow_udtf(f, return_type):
            import pandas as pd

            arrow_return_type = to_arrow_type(return_type)
            return_type_size = len(return_type)

            def verify_result(result):
                if not isinstance(result, pd.DataFrame):
                    raise PySparkTypeError(
                        error_class="INVALID_ARROW_UDTF_RETURN_TYPE",
                        message_parameters={
                            "type_name": type(result).__name__,
                            "value": str(result),
                        },
                    )

                # Validate the output schema when the result dataframe has either output
                # rows or columns. Note that we avoid using `df.empty` here because the
                # result dataframe may contain an empty row. For example, when a UDTF is
                # defined as follows: def eval(self): yield tuple().
                if len(result) > 0 or len(result.columns) > 0:
                    if len(result.columns) != return_type_size:
                        raise PySparkRuntimeError(
                            error_class="UDTF_RETURN_SCHEMA_MISMATCH",
                            message_parameters={
                                "expected": str(return_type_size),
                                "actual": str(len(result.columns)),
                            },
                        )

                # Verify the type and the schema of the result.
                verify_pandas_result(
                    result, return_type, assign_cols_by_name=False, truncate_return_schema=False
                )
                return result

            # Wrap the exception thrown from the UDTF in a PySparkRuntimeError.
            def func(*a: Any, **kw: Any) -> Any:
                try:
                    return f(*a, **kw)
                except Exception as e:
                    raise PySparkRuntimeError(
                        error_class="UDTF_EXEC_ERROR",
                        message_parameters={"method_name": f.__name__, "error": str(e)},
                    )

            def evaluate(*args: pd.Series, **kwargs: pd.Series):
                if len(args) == 0 and len(kwargs) == 0:
                    yield verify_result(pd.DataFrame(func())), arrow_return_type
                else:
                    # Create tuples from the input pandas Series, each tuple
                    # represents a row across all Series.
                    keys = list(kwargs.keys())
                    len_args = len(args)
                    row_tuples = zip(*args, *[kwargs[key] for key in keys])
                    for row in row_tuples:
                        res = func(
                            *row[:len_args],
                            **{key: row[len_args + i] for i, key in enumerate(keys)},
                        )
                        if res is not None and not isinstance(res, Iterable):
                            raise PySparkRuntimeError(
                                error_class="UDTF_RETURN_NOT_ITERABLE",
                                message_parameters={
                                    "type": type(res).__name__,
                                },
                            )
                        yield verify_result(pd.DataFrame(res)), arrow_return_type

            return evaluate

        eval = wrap_arrow_udtf(getattr(udtf, "eval"), return_type)

        if hasattr(udtf, "terminate"):
            terminate = wrap_arrow_udtf(getattr(udtf, "terminate"), return_type)
        else:
            terminate = None

        def mapper(_, it):
            try:
                for a in it:
                    # The eval function yields an iterator. Each element produced by this
                    # iterator is a tuple in the form of (pandas.DataFrame, arrow_return_type).
                    yield from eval(
                        *[a[o] for o in args_offsets],
                        **{k: a[o] for k, o in kwargs_offsets.items()},
                    )
            finally:
                if terminate is not None:
                    yield from terminate()

        return mapper, None, ser, ser

    else:

        def wrap_udtf(f, return_type):
            assert return_type.needConversion()
            toInternal = return_type.toInternal
            return_type_size = len(return_type)

            def verify_and_convert_result(result):
                if result is not None:
                    if hasattr(result, "__len__") and len(result) != return_type_size:
                        raise PySparkRuntimeError(
                            error_class="UDTF_RETURN_SCHEMA_MISMATCH",
                            message_parameters={
                                "expected": str(return_type_size),
                                "actual": str(len(result)),
                            },
                        )

                    if not (isinstance(result, (list, dict, tuple)) or hasattr(result, "__dict__")):
                        raise PySparkRuntimeError(
                            error_class="UDTF_INVALID_OUTPUT_ROW_TYPE",
                            message_parameters={"type": type(result).__name__},
                        )

                return toInternal(result)

            # Evaluate the function and return a tuple back to the executor.
            def evaluate(*a, **kw) -> tuple:
                try:
                    res = f(*a, **kw)
                except Exception as e:
                    raise PySparkRuntimeError(
                        error_class="UDTF_EXEC_ERROR",
                        message_parameters={"method_name": f.__name__, "error": str(e)},
                    )

                if res is None:
                    # If the function returns None or does not have an explicit return statement,
                    # an empty tuple is returned to the executor.
                    # This is because directly constructing tuple(None) results in an exception.
                    return tuple()

                if not isinstance(res, Iterable):
                    raise PySparkRuntimeError(
                        error_class="UDTF_RETURN_NOT_ITERABLE",
                        message_parameters={"type": type(res).__name__},
                    )

                # If the function returns a result, we map it to the internal representation and
                # returns the results as a tuple.
                return tuple(map(verify_and_convert_result, res))

            return evaluate

        eval = wrap_udtf(getattr(udtf, "eval"), return_type)

        if hasattr(udtf, "terminate"):
            terminate = wrap_udtf(getattr(udtf, "terminate"), return_type)
        else:
            terminate = None

        # Return an iterator of iterators.
        def mapper(_, it):
            try:
                for a in it:
                    yield eval(
                        *[a[o] for o in args_offsets],
                        **{k: a[o] for k, o in kwargs_offsets.items()},
                    )
            finally:
                if terminate is not None:
                    yield terminate()

        return mapper, None, ser, ser


def read_udfs(pickleSer, infile, eval_type):
    runner_conf = {}

    if eval_type in (
        PythonEvalType.SQL_ARROW_BATCHED_UDF,
        PythonEvalType.SQL_SCALAR_PANDAS_UDF,
        PythonEvalType.SQL_COGROUPED_MAP_PANDAS_UDF,
        PythonEvalType.SQL_SCALAR_PANDAS_ITER_UDF,
        PythonEvalType.SQL_MAP_PANDAS_ITER_UDF,
        PythonEvalType.SQL_MAP_ARROW_ITER_UDF,
        PythonEvalType.SQL_GROUPED_MAP_PANDAS_UDF,
        PythonEvalType.SQL_GROUPED_AGG_PANDAS_UDF,
        PythonEvalType.SQL_WINDOW_AGG_PANDAS_UDF,
        PythonEvalType.SQL_GROUPED_MAP_PANDAS_UDF_WITH_STATE,
    ):

        # Load conf used for pandas_udf evaluation
        num_conf = read_int(infile)
        for i in range(num_conf):
            k = utf8_deserializer.loads(infile)
            v = utf8_deserializer.loads(infile)
            runner_conf[k] = v

        state_object_schema = None
        if eval_type == PythonEvalType.SQL_GROUPED_MAP_PANDAS_UDF_WITH_STATE:
            state_object_schema = StructType.fromJson(json.loads(utf8_deserializer.loads(infile)))

        # NOTE: if timezone is set here, that implies respectSessionTimeZone is True
        timezone = runner_conf.get("spark.sql.session.timeZone", None)
        safecheck = (
            runner_conf.get("spark.sql.execution.pandas.convertToArrowArraySafely", "false").lower()
            == "true"
        )

        if eval_type == PythonEvalType.SQL_COGROUPED_MAP_PANDAS_UDF:
            ser = CogroupUDFSerializer(timezone, safecheck, assign_cols_by_name(runner_conf))
        elif eval_type == PythonEvalType.SQL_GROUPED_MAP_PANDAS_UDF_WITH_STATE:
            arrow_max_records_per_batch = runner_conf.get(
                "spark.sql.execution.arrow.maxRecordsPerBatch", 10000
            )
            arrow_max_records_per_batch = int(arrow_max_records_per_batch)

            ser = ApplyInPandasWithStateSerializer(
                timezone,
                safecheck,
                assign_cols_by_name(runner_conf),
                state_object_schema,
                arrow_max_records_per_batch,
            )
        elif eval_type == PythonEvalType.SQL_MAP_ARROW_ITER_UDF:
            ser = ArrowStreamUDFSerializer()
        else:
            # Scalar Pandas UDF handles struct type arguments as pandas DataFrames instead of
            # pandas Series. See SPARK-27240.
            df_for_struct = (
                eval_type == PythonEvalType.SQL_SCALAR_PANDAS_UDF
                or eval_type == PythonEvalType.SQL_SCALAR_PANDAS_ITER_UDF
                or eval_type == PythonEvalType.SQL_MAP_PANDAS_ITER_UDF
            )
            # Arrow-optimized Python UDF takes a struct type argument as a Row
            struct_in_pandas = (
                "row" if eval_type == PythonEvalType.SQL_ARROW_BATCHED_UDF else "dict"
            )
            ndarray_as_list = eval_type == PythonEvalType.SQL_ARROW_BATCHED_UDF
            # Arrow-optimized Python UDF uses explicit Arrow cast for type coercion
            arrow_cast = eval_type == PythonEvalType.SQL_ARROW_BATCHED_UDF
            ser = ArrowStreamPandasUDFSerializer(
                timezone,
                safecheck,
                assign_cols_by_name(runner_conf),
                df_for_struct,
                struct_in_pandas,
                ndarray_as_list,
                arrow_cast,
            )
    else:
        ser = BatchedSerializer(CPickleSerializer(), 100)

    num_udfs = read_int(infile)

    is_scalar_iter = eval_type == PythonEvalType.SQL_SCALAR_PANDAS_ITER_UDF
    is_map_pandas_iter = eval_type == PythonEvalType.SQL_MAP_PANDAS_ITER_UDF
    is_map_arrow_iter = eval_type == PythonEvalType.SQL_MAP_ARROW_ITER_UDF

    if is_scalar_iter or is_map_pandas_iter or is_map_arrow_iter:
        if is_scalar_iter:
            assert num_udfs == 1, "One SCALAR_ITER UDF expected here."
        if is_map_pandas_iter:
            assert num_udfs == 1, "One MAP_PANDAS_ITER UDF expected here."
        if is_map_arrow_iter:
            assert num_udfs == 1, "One MAP_ARROW_ITER UDF expected here."

        arg_offsets, _, udf = read_single_udf(
            pickleSer, infile, eval_type, runner_conf, udf_index=0
        )

        def func(_, iterator):
            num_input_rows = 0

            def map_batch(batch):
                nonlocal num_input_rows

                udf_args = [batch[offset] for offset in arg_offsets]
                num_input_rows += len(udf_args[0])
                if len(udf_args) == 1:
                    return udf_args[0]
                else:
                    return tuple(udf_args)

            iterator = map(map_batch, iterator)
            result_iter = udf(iterator)

            num_output_rows = 0
            for result_batch, result_type in result_iter:
                num_output_rows += len(result_batch)
                # This assert is for Scalar Iterator UDF to fail fast.
                # The length of the entire input can only be explicitly known
                # by consuming the input iterator in user side. Therefore,
                # it's very unlikely the output length is higher than
                # input length.
                assert (
                    is_map_pandas_iter or is_map_arrow_iter or num_output_rows <= num_input_rows
                ), "Pandas SCALAR_ITER UDF outputted more rows than input rows."
                yield (result_batch, result_type)

            if is_scalar_iter:
                try:
                    next(iterator)
                except StopIteration:
                    pass
                else:
                    raise PySparkRuntimeError(
                        error_class="STOP_ITERATION_OCCURRED_FROM_SCALAR_ITER_PANDAS_UDF",
                        message_parameters={},
                    )

                if num_output_rows != num_input_rows:
                    raise PySparkRuntimeError(
                        error_class="RESULT_LENGTH_MISMATCH_FOR_SCALAR_ITER_PANDAS_UDF",
                        message_parameters={
                            "output_length": str(num_output_rows),
                            "input_length": str(num_input_rows),
                        },
                    )

        # profiling is not supported for UDF
        return func, None, ser, ser

    def extract_key_value_indexes(grouped_arg_offsets):
        """
        Helper function to extract the key and value indexes from arg_offsets for the grouped and
        cogrouped pandas udfs. See BasePandasGroupExec.resolveArgOffsets for equivalent scala code.

        Parameters
        ----------
        grouped_arg_offsets:  list
            List containing the key and value indexes of columns of the
            DataFrames to be passed to the udf. It consists of n repeating groups where n is the
            number of DataFrames.  Each group has the following format:
                group[0]: length of group
                group[1]: length of key indexes
                group[2.. group[1] +2]: key attributes
                group[group[1] +3 group[0]]: value attributes
        """
        parsed = []
        idx = 0
        while idx < len(grouped_arg_offsets):
            offsets_len = grouped_arg_offsets[idx]
            idx += 1
            offsets = grouped_arg_offsets[idx : idx + offsets_len]
            split_index = offsets[0] + 1
            offset_keys = offsets[1:split_index]
            offset_values = offsets[split_index:]
            parsed.append([offset_keys, offset_values])
            idx += offsets_len
        return parsed

    if eval_type == PythonEvalType.SQL_GROUPED_MAP_PANDAS_UDF:
        # We assume there is only one UDF here because grouped map doesn't
        # support combining multiple UDFs.
        assert num_udfs == 1

        # See FlatMapGroupsInPandasExec for how arg_offsets are used to
        # distinguish between grouping attributes and data attributes
        arg_offsets, _, f = read_single_udf(pickleSer, infile, eval_type, runner_conf, udf_index=0)
        parsed_offsets = extract_key_value_indexes(arg_offsets)

        # Create function like this:
        #   mapper a: f([a[0]], [a[0], a[1]])
        def mapper(a):
            keys = [a[o] for o in parsed_offsets[0][0]]
            vals = [a[o] for o in parsed_offsets[0][1]]
            return f(keys, vals)

    elif eval_type == PythonEvalType.SQL_GROUPED_MAP_PANDAS_UDF_WITH_STATE:
        # We assume there is only one UDF here because grouped map doesn't
        # support combining multiple UDFs.
        assert num_udfs == 1

        # See FlatMapGroupsInPandas(WithState)Exec for how arg_offsets are used to
        # distinguish between grouping attributes and data attributes
        arg_offsets, _, f = read_single_udf(pickleSer, infile, eval_type, runner_conf, udf_index=0)
        parsed_offsets = extract_key_value_indexes(arg_offsets)

        def mapper(a):
            """
            The function receives (iterator of data, state) and performs extraction of key and
            value from the data, with retaining lazy evaluation.

            See `load_stream` in `ApplyInPandasWithStateSerializer` for more details on the input
            and see `wrap_grouped_map_pandas_udf_with_state` for more details on how output will
            be used.
            """
            from itertools import tee

            state = a[1]
            data_gen = (x[0] for x in a[0])

            # We know there should be at least one item in the iterator/generator.
            # We want to peek the first element to construct the key, hence applying
            # tee to construct the key while we retain another iterator/generator
            # for values.
            keys_gen, values_gen = tee(data_gen)
            keys_elem = next(keys_gen)
            keys = [keys_elem[o] for o in parsed_offsets[0][0]]

            # This must be generator comprehension - do not materialize.
            vals = ([x[o] for o in parsed_offsets[0][1]] for x in values_gen)

            return f(keys, vals, state)

    elif eval_type == PythonEvalType.SQL_COGROUPED_MAP_PANDAS_UDF:
        # We assume there is only one UDF here because cogrouped map doesn't
        # support combining multiple UDFs.
        assert num_udfs == 1
        arg_offsets, _, f = read_single_udf(pickleSer, infile, eval_type, runner_conf, udf_index=0)

        parsed_offsets = extract_key_value_indexes(arg_offsets)

        def mapper(a):
            df1_keys = [a[0][o] for o in parsed_offsets[0][0]]
            df1_vals = [a[0][o] for o in parsed_offsets[0][1]]
            df2_keys = [a[1][o] for o in parsed_offsets[1][0]]
            df2_vals = [a[1][o] for o in parsed_offsets[1][1]]
            return f(df1_keys, df1_vals, df2_keys, df2_vals)

    else:
        udfs = []
        for i in range(num_udfs):
            udfs.append(read_single_udf(pickleSer, infile, eval_type, runner_conf, udf_index=i))

        def mapper(a):
            result = tuple(
                f(*[a[o] for o in args_offsets], **{k: a[o] for k, o in kwargs_offsets.items()})
                for args_offsets, kwargs_offsets, f in udfs
            )
            # In the special case of a single UDF this will return a single result rather
            # than a tuple of results; this is the format that the JVM side expects.
            if len(result) == 1:
                return result[0]
            else:
                return result

    def func(_, it):
        return map(mapper, it)

    # profiling is not supported for UDF
    return func, None, ser, ser


def main(infile, outfile):
    faulthandler_log_path = os.environ.get("PYTHON_FAULTHANDLER_DIR", None)
    try:
        if faulthandler_log_path:
            faulthandler_log_path = os.path.join(faulthandler_log_path, str(os.getpid()))
            faulthandler_log_file = open(faulthandler_log_path, "w")
            faulthandler.enable(file=faulthandler_log_file)

        boot_time = time.time()
        split_index = read_int(infile)
        if split_index == -1:  # for unit tests
            sys.exit(-1)

        check_python_version(infile)

        # read inputs only for a barrier task
        isBarrier = read_bool(infile)
        boundPort = read_int(infile)
        secret = UTF8Deserializer().loads(infile)

        memory_limit_mb = int(os.environ.get("PYSPARK_EXECUTOR_MEMORY_MB", "-1"))
        setup_memory_limits(memory_limit_mb)

        # initialize global state
        taskContext = None
        if isBarrier:
            taskContext = BarrierTaskContext._getOrCreate()
            BarrierTaskContext._initialize(boundPort, secret)
            # Set the task context instance here, so we can get it by TaskContext.get for
            # both TaskContext and BarrierTaskContext
            TaskContext._setTaskContext(taskContext)
        else:
            taskContext = TaskContext._getOrCreate()
        # read inputs for TaskContext info
        taskContext._stageId = read_int(infile)
        taskContext._partitionId = read_int(infile)
        taskContext._attemptNumber = read_int(infile)
        taskContext._taskAttemptId = read_long(infile)
        taskContext._cpus = read_int(infile)
        taskContext._resources = {}
        for r in range(read_int(infile)):
            key = utf8_deserializer.loads(infile)
            name = utf8_deserializer.loads(infile)
            addresses = []
            taskContext._resources = {}
            for a in range(read_int(infile)):
                addresses.append(utf8_deserializer.loads(infile))
            taskContext._resources[key] = ResourceInformation(name, addresses)

        taskContext._localProperties = dict()
        for i in range(read_int(infile)):
            k = utf8_deserializer.loads(infile)
            v = utf8_deserializer.loads(infile)
            taskContext._localProperties[k] = v

        shuffle.MemoryBytesSpilled = 0
        shuffle.DiskBytesSpilled = 0
        _accumulatorRegistry.clear()

        setup_spark_files(infile)
        setup_broadcasts(infile)

        _accumulatorRegistry.clear()
        eval_type = read_int(infile)
        if eval_type == PythonEvalType.NON_UDF:
            func, profiler, deserializer, serializer = read_command(pickleSer, infile)
        elif eval_type in (PythonEvalType.SQL_TABLE_UDF, PythonEvalType.SQL_ARROW_TABLE_UDF):
            func, profiler, deserializer, serializer = read_udtf(pickleSer, infile, eval_type)
        else:
            func, profiler, deserializer, serializer = read_udfs(pickleSer, infile, eval_type)

        init_time = time.time()

        def process():
            iterator = deserializer.load_stream(infile)
            out_iter = func(split_index, iterator)
            try:
                serializer.dump_stream(out_iter, outfile)
            finally:
                if hasattr(out_iter, "close"):
                    out_iter.close()

        if profiler:
            profiler.profile(process)
        else:
            process()

        # Reset task context to None. This is a guard code to avoid residual context when worker
        # reuse.
        TaskContext._setTaskContext(None)
        BarrierTaskContext._setTaskContext(None)
    except BaseException as e:
        try:
            exc_info = None
            if os.environ.get("SPARK_SIMPLIFIED_TRACEBACK", False):
                tb = try_simplify_traceback(sys.exc_info()[-1])
                if tb is not None:
                    e.__cause__ = None
                    exc_info = "".join(traceback.format_exception(type(e), e, tb))
            if exc_info is None:
                exc_info = traceback.format_exc()

            write_int(SpecialLengths.PYTHON_EXCEPTION_THROWN, outfile)
            write_with_length(exc_info.encode("utf-8"), outfile)
        except IOError:
            # JVM close the socket
            pass
        except BaseException:
            # Write the error to stderr if it happened while serializing
            print("PySpark worker failed with exception:", file=sys.stderr)
            print(traceback.format_exc(), file=sys.stderr)
        sys.exit(-1)
    finally:
        if faulthandler_log_path:
            faulthandler.disable()
            faulthandler_log_file.close()
            os.remove(faulthandler_log_path)
    finish_time = time.time()
    report_times(outfile, boot_time, init_time, finish_time)
    write_long(shuffle.MemoryBytesSpilled, outfile)
    write_long(shuffle.DiskBytesSpilled, outfile)

    # Mark the beginning of the accumulators section of the output
    write_int(SpecialLengths.END_OF_DATA_SECTION, outfile)
    send_accumulator_updates(outfile)

    # check end of stream
    if read_int(infile) == SpecialLengths.END_OF_STREAM:
        write_int(SpecialLengths.END_OF_STREAM, outfile)
    else:
        # write a different value to tell JVM to not reuse this worker
        write_int(SpecialLengths.END_OF_DATA_SECTION, outfile)
        sys.exit(-1)


if __name__ == "__main__":
    # Read information about how to connect back to the JVM from the environment.
    java_port = int(os.environ["PYTHON_WORKER_FACTORY_PORT"])
    auth_secret = os.environ["PYTHON_WORKER_FACTORY_SECRET"]
    (sock_file, _) = local_connect_and_auth(java_port, auth_secret)
    # TODO: Remove the following two lines and use `Process.pid()` when we drop JDK 8.
    write_int(os.getpid(), sock_file)
    sock_file.flush()
    main(sock_file, sock_file)

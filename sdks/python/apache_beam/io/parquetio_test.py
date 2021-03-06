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
from __future__ import absolute_import

import json
import logging
import os
import shutil
import sys
import tempfile
import unittest

import hamcrest as hc
import pandas
import pyarrow as pa
import pyarrow.lib as pl
import pyarrow.parquet as pq
from parameterized import param
from parameterized import parameterized

from apache_beam import Create
from apache_beam import Map
from apache_beam.io import filebasedsource
from apache_beam.io import source_test_utils
from apache_beam.io.iobase import RangeTracker
from apache_beam.io.parquetio import ReadAllFromParquet
from apache_beam.io.parquetio import ReadFromParquet
from apache_beam.io.parquetio import WriteToParquet
from apache_beam.io.parquetio import _create_parquet_sink
from apache_beam.io.parquetio import _create_parquet_source
from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that
from apache_beam.testing.util import equal_to
from apache_beam.transforms.display import DisplayData
from apache_beam.transforms.display_test import DisplayDataItemMatcher


class TestParquet(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    # Method has been renamed in Python 3
    if sys.version_info[0] < 3:
      cls.assertCountEqual = cls.assertItemsEqual

  def setUp(self):
    # Reducing the size of thread pools. Without this test execution may fail in
    # environments with limited amount of resources.
    filebasedsource.MAX_NUM_THREADS_FOR_SIZE_ESTIMATION = 2
    self.temp_dir = tempfile.mkdtemp()

  def tearDown(self):
    shutil.rmtree(self.temp_dir)

  RECORDS = [{'name': 'Thomas',
              'favorite_number': 1,
              'favorite_color': 'blue'}, {'name': 'Henry',
                                          'favorite_number': 3,
                                          'favorite_color': 'green'},
             {'name': 'Toby',
              'favorite_number': 7,
              'favorite_color': 'brown'}, {'name': 'Gordon',
                                           'favorite_number': 4,
                                           'favorite_color': 'blue'},
             {'name': 'Emily',
              'favorite_number': -1,
              'favorite_color': 'Red'}, {'name': 'Percy',
                                         'favorite_number': 6,
                                         'favorite_color': 'Green'}]

  SCHEMA = pa.schema([
      ('name', pa.binary()),
      ('favorite_number', pa.int64()),
      ('favorite_color', pa.binary())
  ])

  SCHEMA96 = pa.schema([
      ('name', pa.binary()),
      ('favorite_number', pa.timestamp('ns')),
      ('favorite_color', pa.binary())
  ])

  def _record_to_columns(self, records, schema):
    col_list = []
    for n in schema.names:
      column = []
      for r in records:
        column.append(r[n])
      col_list.append(column)
    return col_list

  def _write_data(self,
                  directory=None,
                  schema=None,
                  prefix=tempfile.template,
                  row_group_size=1000,
                  codec='none',
                  count=len(RECORDS)):
    if schema is None:
      schema = self.SCHEMA

    if directory is None:
      directory = self.temp_dir

    with tempfile.NamedTemporaryFile(
        delete=False, dir=directory, prefix=prefix) as f:
      len_records = len(self.RECORDS)
      data = []
      for i in range(count):
        data.append(self.RECORDS[i % len_records])
      col_data = self._record_to_columns(data, schema)
      col_array = [
          pa.array(c, schema.types[cn]) for cn, c in enumerate(col_data)
      ]
      table = pa.Table.from_arrays(col_array, schema.names)
      pq.write_table(
          table, f, row_group_size=row_group_size, compression=codec,
          use_deprecated_int96_timestamps=True
      )

      return f.name

  def _write_pattern(self, num_files):
    assert num_files > 0
    temp_dir = tempfile.mkdtemp(dir=self.temp_dir)

    for _ in range(num_files):
      self._write_data(directory=temp_dir, prefix='mytemp')

    return temp_dir + os.path.sep + 'mytemp*'

  def _run_parquet_test(self, pattern, columns, desired_bundle_size,
                        perform_splitting, expected_result):
    source = _create_parquet_source(pattern, columns=columns)
    if perform_splitting:
      assert desired_bundle_size
      sources_info = [
          (split.source, split.start_position, split.stop_position)
          for split in source.split(desired_bundle_size=desired_bundle_size)
      ]
      if len(sources_info) < 2:
        raise ValueError('Test is trivial. Please adjust it so that at least '
                         'two splits get generated')

      source_test_utils.assert_sources_equal_reference_source(
          (source, None, None), sources_info)
    else:
      read_records = source_test_utils.read_from_source(source, None, None)
      self.assertCountEqual(expected_result, read_records)

  def test_read_without_splitting(self):
    file_name = self._write_data()
    expected_result = self.RECORDS
    self._run_parquet_test(file_name, None, None, False, expected_result)

  def test_read_with_splitting(self):
    file_name = self._write_data()
    expected_result = self.RECORDS
    self._run_parquet_test(file_name, None, 100, True, expected_result)

  def test_source_display_data(self):
    file_name = 'some_parquet_source'
    source = \
        _create_parquet_source(
            file_name,
            validate=False
        )
    dd = DisplayData.create_from(source)

    expected_items = [
        DisplayDataItemMatcher('compression', 'auto'),
        DisplayDataItemMatcher('file_pattern', file_name)]
    hc.assert_that(dd.items, hc.contains_inanyorder(*expected_items))

  def test_read_display_data(self):
    file_name = 'some_parquet_source'
    read = \
      ReadFromParquet(
          file_name,
          validate=False)
    dd = DisplayData.create_from(read)

    expected_items = [
        DisplayDataItemMatcher('compression', 'auto'),
        DisplayDataItemMatcher('file_pattern', file_name)]
    hc.assert_that(dd.items, hc.contains_inanyorder(*expected_items))

  def test_sink_display_data(self):
    file_name = 'some_parquet_sink'
    sink = _create_parquet_sink(
        file_name,
        self.SCHEMA,
        'none',
        1024*1024,
        1000,
        False,
        '.end',
        0,
        None,
        'application/x-parquet')
    dd = DisplayData.create_from(sink)
    expected_items = [
        DisplayDataItemMatcher(
            'schema',
            str(self.SCHEMA)),
        DisplayDataItemMatcher(
            'file_pattern',
            'some_parquet_sink-%(shard_num)05d-of-%(num_shards)05d.end'),
        DisplayDataItemMatcher(
            'codec',
            'none'),
        DisplayDataItemMatcher(
            'row_group_buffer_size',
            str(1024*1024)),
        DisplayDataItemMatcher(
            'compression',
            'uncompressed')]
    hc.assert_that(dd.items, hc.contains_inanyorder(*expected_items))

  def test_write_display_data(self):
    file_name = 'some_parquet_sink'
    write = WriteToParquet(file_name, self.SCHEMA)
    dd = DisplayData.create_from(write)
    expected_items = [
        DisplayDataItemMatcher(
            'codec',
            'none'),
        DisplayDataItemMatcher(
            'schema',
            str(self.SCHEMA)),
        DisplayDataItemMatcher(
            'row_group_buffer_size',
            str(64*1024*1024)),
        DisplayDataItemMatcher(
            'file_pattern',
            'some_parquet_sink-%(shard_num)05d-of-%(num_shards)05d'),
        DisplayDataItemMatcher(
            'compression',
            'uncompressed')]
    hc.assert_that(dd.items, hc.contains_inanyorder(*expected_items))

  def test_sink_transform_int96(self):
    with tempfile.NamedTemporaryFile() as dst:
      path = dst.name
      # pylint: disable=c-extension-no-member
      with self.assertRaises(pl.ArrowInvalid):
        with TestPipeline() as p:
          _ = p \
          | Create(self.RECORDS) \
          | WriteToParquet(
              path, self.SCHEMA96, num_shards=1, shard_name_template='')

  def test_sink_transform(self):
    with tempfile.NamedTemporaryFile() as dst:
      path = dst.name
      with TestPipeline() as p:
        _ = p \
        | Create(self.RECORDS) \
        | WriteToParquet(
            path, self.SCHEMA, num_shards=1, shard_name_template='')
      with TestPipeline() as p:
        # json used for stable sortability
        readback = \
            p \
            | ReadFromParquet(path) \
            | Map(json.dumps)
        assert_that(readback, equal_to([json.dumps(r) for r in self.RECORDS]))

  @parameterized.expand([
      param(compression_type='snappy'),
      param(compression_type='gzip'),
      param(compression_type='brotli'),
      param(compression_type='lz4'),
      param(compression_type='zstd')
  ])
  def test_sink_transform_compressed(self, compression_type):
    with tempfile.NamedTemporaryFile() as dst:
      path = dst.name
      with TestPipeline() as p:
        _ = p \
        | Create(self.RECORDS) \
        | WriteToParquet(
            path, self.SCHEMA, codec=compression_type,
            num_shards=1, shard_name_template='')
      with TestPipeline() as p:
        # json used for stable sortability
        readback = \
            p \
            | ReadFromParquet(path + '*') \
            | Map(json.dumps)
        assert_that(readback, equal_to([json.dumps(r) for r in self.RECORDS]))

  def test_read_reentrant(self):
    file_name = self._write_data()
    source = _create_parquet_source(file_name)
    source_test_utils.assert_reentrant_reads_succeed((source, None, None))

  def test_read_without_splitting_multiple_row_group(self):
    file_name = self._write_data(count=12000)
    expected_result = self.RECORDS * 2000
    self._run_parquet_test(file_name, None, None, False, expected_result)

  def test_read_with_splitting_multiple_row_group(self):
    file_name = self._write_data(count=12000)
    expected_result = self.RECORDS * 2000
    self._run_parquet_test(file_name, None, 10000, True, expected_result)

  def test_dynamic_work_rebalancing(self):
    file_name = self._write_data(count=120, row_group_size=20)
    source = _create_parquet_source(file_name)

    splits = [
        split
        for split in source.split(desired_bundle_size=float('inf'))
    ]
    assert len(splits) == 1

    source_test_utils.assert_split_at_fraction_exhaustive(
        splits[0].source, splits[0].start_position, splits[0].stop_position
    )

  def test_min_bundle_size(self):
    file_name = self._write_data(count=120, row_group_size=20)

    source = _create_parquet_source(file_name, min_bundle_size=100*1024*1024)
    splits = [
        split for split in source.split(desired_bundle_size=1)
    ]
    self.assertEquals(len(splits), 1)

    source = _create_parquet_source(file_name, min_bundle_size=0)
    splits = [
        split for split in source.split(desired_bundle_size=1)
    ]
    self.assertNotEquals(len(splits), 1)

  def _convert_to_timestamped_record(self, record):
    timestamped_record = record.copy()
    timestamped_record['favorite_number'] =\
      pandas.Timestamp(timestamped_record['favorite_number'])
    return timestamped_record

  def test_int96_type_conversion(self):
    file_name = self._write_data(
        count=120, row_group_size=20, schema=self.SCHEMA96)
    expected_result = [
        self._convert_to_timestamped_record(x) for x in self.RECORDS
    ] * 20
    self._run_parquet_test(file_name, None, None, False, expected_result)

  def test_split_points(self):
    file_name = self._write_data(count=12000, row_group_size=3000)
    source = _create_parquet_source(file_name)

    splits = [
        split for split in source.split(desired_bundle_size=float('inf'))
    ]
    assert len(splits) == 1

    range_tracker = splits[0].source.get_range_tracker(
        splits[0].start_position, splits[0].stop_position)

    split_points_report = []

    for _ in splits[0].source.read(range_tracker):
      split_points_report.append(range_tracker.split_points())

    # There are a total of four row groups. Each row group has 3000 records.

    # When reading records of the first group, range_tracker.split_points()
    # should return (0, iobase.RangeTracker.SPLIT_POINTS_UNKNOWN)
    self.assertEquals(
        split_points_report[:10],
        [(0, RangeTracker.SPLIT_POINTS_UNKNOWN)] * 10)

    # When reading records of last group, range_tracker.split_points() should
    # return (3, 1)
    self.assertEquals(split_points_report[-10:], [(3, 1)] * 10)

  def test_selective_columns(self):
    file_name = self._write_data()
    expected_result = [{'name': r['name']} for r in self.RECORDS]
    self._run_parquet_test(file_name, ['name'], None, False, expected_result)

  def test_sink_transform_multiple_row_group(self):
    with tempfile.NamedTemporaryFile() as dst:
      path = dst.name
      with TestPipeline() as p:
        # writing 623200 bytes of data
        _ = p \
        | Create(self.RECORDS * 4000) \
        | WriteToParquet(
            path, self.SCHEMA, num_shards=1, codec='none',
            shard_name_template='', row_group_buffer_size=250000)
      self.assertEqual(pq.read_metadata(path).num_row_groups, 3)

  def test_read_all_from_parquet_single_file(self):
    path = self._write_data()
    with TestPipeline() as p:
      assert_that(
          p \
          | Create([path]) \
          | ReadAllFromParquet(),
          equal_to(self.RECORDS))

  def test_read_all_from_parquet_many_single_files(self):
    path1 = self._write_data()
    path2 = self._write_data()
    path3 = self._write_data()
    with TestPipeline() as p:
      assert_that(
          p \
          | Create([path1, path2, path3]) \
          | ReadAllFromParquet(),
          equal_to(self.RECORDS * 3))

  def test_read_all_from_parquet_file_pattern(self):
    file_pattern = self._write_pattern(5)
    with TestPipeline() as p:
      assert_that(
          p \
          | Create([file_pattern]) \
          | ReadAllFromParquet(),
          equal_to(self.RECORDS * 5))

  def test_read_all_from_parquet_many_file_patterns(self):
    file_pattern1 = self._write_pattern(5)
    file_pattern2 = self._write_pattern(2)
    file_pattern3 = self._write_pattern(3)
    with TestPipeline() as p:
      assert_that(
          p \
          | Create([file_pattern1, file_pattern2, file_pattern3]) \
          | ReadAllFromParquet(),
          equal_to(self.RECORDS * 10))


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  unittest.main()

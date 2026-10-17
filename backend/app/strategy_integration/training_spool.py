"""Bounded, job-owned binary training partitions; never a database or cache."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np


class TrainingRows:
    def __init__(self, root, dimension):
        self.root, self.dimension, self.count = Path(root), dimension, 0
        self.label_counts = [0, 0]
        self.digest = hashlib.sha256()

    def __len__(self):
        return self.count

    def append(self, row):
        x = np.asarray(row['features'], dtype='<f4')
        if x.shape != (self.dimension,) or row['label'] not in (0, 1):
            raise ValueError('TRAINING_PARTITION_SCHEMA')
        y = bytes([row['label']])
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root/'features.f32').open('ab') as stream:
            stream.write(x.tobytes())
        with (self.root/'labels.u8').open('ab') as stream:
            stream.write(y)
        self.digest.update(row['sample_id'].encode()+b'\n'+x.tobytes()+y)
        self.count += 1
        self.label_counts[row['label']] += 1

    def as_arrays(self):
        if not self.count:
            raise ValueError('TRAINING_PARTITION_EMPTY')
        if (self.root/'features.f32').stat().st_size != self.count*self.dimension*4 or (self.root/'labels.u8').stat().st_size != self.count:
            raise ValueError('TRAINING_PARTITION_TRUNCATED')
        return (np.memmap(self.root/'features.f32', dtype='<f4', mode='r', shape=(self.count, self.dimension)),
                np.memmap(self.root/'labels.u8', dtype='u1', mode='r', shape=(self.count,)))


class DatasetSpool:
    def __init__(self, root, dimension):
        self.root, self.dimension, self.partitions = Path(root), dimension, {}

    def append(self, key, row):
        if key not in self.partitions:
            self.partitions[key] = TrainingRows(self.root/'_'.join(map(str, key)), self.dimension)
        self.partitions[key].append(row)

    def manifest(self):
        return {'_'.join(map(str, key)): {'n': len(rows), 'class_counts': rows.label_counts,
                'sample_feature_label_sha256': rows.digest.hexdigest()} for key, rows in self.partitions.items()}

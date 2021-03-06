
import numpy
from EngineBatch import Batch
from Log import log
from Util import NumbersDict


def assign_dev_data(device, dataset, batches, load_seqs=True):
  """
  :type device: Device.Device
  :type dataset: Dataset.Dataset
  :type batches: list[EngineBatch.Batch]
  :returns successful and how much batch idx to advance.
  :rtype: (bool,int)
  """
  shapes = dataset.shapes_for_batches(batches, data_keys=device.used_data_keys)
  if shapes is None:
    return False, len(batches)
  device.alloc_data(shapes=shapes, max_ctc_length=dataset.get_max_ctc_length())
  offset_slice = 0

  for batch in batches:
    if load_seqs: dataset.load_seqs(batch.start_seq, batch.end_seq)
    device.num_frames += batch.get_total_num_frames()
    with dataset.lock:
      for seq in batch.seqs:
        o = seq.batch_frame_offset
        q = seq.batch_slice + offset_slice
        l = seq.frame_length
        # input-data, input-index will also be set in this loop. That is data-key "data".
        for k in device.used_data_keys:
          if l[k] == 0: continue
          data = dataset.get_data_slice(seq.seq_idx, k, seq.seq_start_frame[k], seq.seq_end_frame[k])
          ls = data.shape[0]
          if "[sparse:" in k:
            assert o[k] == 0, "sparse non-recurrent batching + chunking not implemented"
            _device_maybe_enlarge_data(device, k, ls)
          else:
            if ls != l[k]:
              raise Exception("got shape[0]: %i, expected: %i, start/end: %r/%r, seq_idx: %i, seq len: %r" % (
                ls, l[k], seq.seq_start_frame, seq.seq_end_frame, seq.seq_idx, dataset.get_seq_length(seq.seq_idx)))
          device.output_index[k][o[k]:o[k] + ls, q] = numpy.ones((ls,), dtype='int8')
          device.targets[k][o[k]:o[k] + ls, q] = data
        # Only copy ctc targets if chunking is inactive to avoid out of range access.
        # CTC is not compatible with chunking anyway.
        chunking_active = dataset.chunk_size > 0
        if dataset.has_ctc_targets() and not chunking_active:
          device.ctc_targets[q] = dataset.get_ctc_targets(seq.seq_idx)

        device.tags[q] = dataset.get_tag(seq.seq_idx)
    # Note on multiple batches for the non-recurrent case:
    # We could either concatenate all into a single slice, or do multiple slices.
    # We do multiple slices here.
    # See also the `shape` calculation above.
    offset_slice += batch.num_slices

  return True, len(batches)


def _device_maybe_enlarge_data(device, key, needed_len):
  cur_len = device.output_index[key].shape[0]
  if cur_len >= needed_len:
    return
  diff_len = needed_len - cur_len
  new_len = cur_len + int(diff_len * 1.5)  # a bit more than needed
  assert new_len >= needed_len
  # Also see Device.alloc_data() for reference.
  # First, new output_index.
  old_index = device.output_index[key]
  index_shape = list(old_index.shape)
  index_shape[0] = new_len
  device.output_index[key] = numpy.zeros(index_shape, dtype='int8')
  device.output_index[key][0:cur_len] = old_index
  # Now, new targets.
  old_targets = device.targets[key]
  targets_shape = list(old_targets.shape)
  targets_shape[0] = new_len
  device.targets[key] = numpy.full(targets_shape, -1, dtype=device.targets[key].dtype)
  device.targets[key][0:cur_len] = old_targets


def assign_dev_data_single_seq(device, dataset, seq, load_seqs=True):
  """
  :type device: Device.Device
  :type dataset: Dataset.Dataset
  :param int seq: sorted seq idx
  :return: whether we succeeded
  :rtype: bool
  """
  batch = Batch()
  batch.add_frames(seq_idx=seq, seq_start_frame=0, length=dataset.get_seq_length(seq))
  success, _ = assign_dev_data(device, dataset, [batch], load_seqs=load_seqs)
  return success


def maybe_subtract_priors(network, train, config):
  """
  :type network: Network.LayerNetwork
  :type train: Dataset.Dataset
  :type config: Config.Config
  """
  if config.bool('subtract_priors', False):
    prior_scale = config.float('prior_scale', 0.0)
    priors = train.calculate_priori()
    priors[priors == 0] = 1e-10 #avoid priors of zero which would yield a bias of inf
    l = [p for p in network.train_params_vars if p.name == 'b_output']
    assert len(l) == 1, len(l)
    b_softmax = l[0]
    b_softmax.set_value(b_softmax.get_value() - prior_scale * numpy.log(priors))
    print >> log.v3, "subtracting priors with prior_scale", prior_scale

"""
Functions to generate pick-corrections for events detected by correlation.


"""
import numpy as np
from eqcorrscan.core.match_filter import normxcorr2


def _channel_loop(detection, template, min_cc, i=0, debug=0):
    """
    Inner loop for correlating and assigning picks.

    Utility function to take a stream of data for the detected event and write
    maximum correlation to absolute time as picks in an obspy.core.event.Event
    object.
    Only outputs picks for picks above min_cc.

    :type detection: obspy.core.stream.Stream
    :param detection: Stream of data for the slave event detected using \
        template.
    :type template: obspy.core.stream.Stream
    :param template: Stream of data as the template for the detection.
    :type i: int
    :param i: Used to track which process has occurred when running in \
        parallel.

    :returns: Event object containing net, sta, chan information
    :rtype: obspy.core.event.Event
    """
    from obspy.core.event import Event, Pick, WaveformStreamID
    from obspy.core.event import ResourceIdentifier
    event = Event()
    s_stachans = {}
    used_s_sta = []
    for tr in template:
        temp_net = tr.stats.network
        temp_sta = tr.stats.station
        temp_chan = tr.stats.channel
        image = detection.select(station=temp_sta,
                                 channel=temp_chan)
        if image:
            ccc = normxcorr2(tr.data, image[0].data)
            # Convert the maximum cross-correlation time to an actual time
            if debug > 3:
                print('********DEBUG: Maximum cross-corr=%s' % np.amax(ccc))
            if np.amax(ccc) > min_cc:
                picktime = image[0].stats.starttime + (np.argmax(ccc) *
                                                       image[0].stats.delta)
            else:
                continue
            # Perhaps weight each pick by the cc val or cc val^2?
            # weight = np.amax(ccc) ** 2
            if temp_chan[-1:] == 'Z':
                phase = 'P'
            # Only take the S-pick with the best correlation
            elif temp_chan[-1:] in ['E', 'N']:
                phase = 'S'
                if temp_sta not in s_stachans and np.amax(ccc) > min_cc:
                    s_stachans[temp_sta] = ((temp_chan, np.amax(ccc),
                                             picktime))
                elif temp_sta in s_stachans and np.amax(ccc) > min_cc:
                    if np.amax(ccc) > s_stachans[temp_sta][1]:
                        picktime = picktime
                    else:
                        picktime = s_stachans[temp_sta][2]
                        temp_chan = s_stachans[temp_sta][0]
                elif np.amax(ccc) < min_cc and temp_sta not in used_s_sta:
                    used_s_sta.append(temp_sta)
                else:
                    continue
            else:
                phase = None
            _waveform_id = WaveformStreamID(network_code=temp_net,
                                            station_code=temp_sta,
                                            channel_code=temp_chan)
            event.picks.append(Pick(waveform_id=_waveform_id,
                                    time=picktime,
                                    method_id=ResourceIdentifier('EQcorrscan'),
                                    phase_hint=phase))
    return (i, event)


def _day_loop(detection_streams, template, min_cc):
    """
    Function to loop through multiple detections for one template.

    Designed to run for the same day of data for I/O simplicity, but as you
    are passing stream objects it could run for all the detections ever, as
    long as you have the RAM!

    :type detection_streams: list
    :param detection_streams: List of all the detections for this template that
        you want to compute the optimum pick for. Individual things in list
        should be of obspy.core.stream.Stream type.
    :type template: obspy.core.stream.Stream
    :param template: The original template used to detect the detections passed
    :type min_cc: float
    :param min_cc: Minimum cross-correlation value to be allowed for a pick.

    :returns: Catalog object containing Event objects for each detection
              created by this template.
    :rtype: obspy.core.event.Catalog
    """
    from multiprocessing import Pool, cpu_count
    # Used to run detections in parallel
    from obspy.core.event import Catalog
    num_cores = cpu_count()
    if num_cores > len(detection_streams):
        num_cores = len(detection_streams)
    pool = Pool(processes=num_cores)
    # Parallelize generation of events for each detection:
    # results is a list of (i, event class)
    results = [pool.apply_async(_channel_loop, args=(detection_streams[i],
                                                     template, min_cc, i))
               for i in xrange(len(detection_streams))]
    pool.close()
    events_list = [p.get() for p in results]
    events_list.sort(key=lambda tup: tup[0])  # Sort based on i.
    temp_catalog = Catalog()
    temp_catalog.events = [event_tup[1] for event_tup in events_list]
    return temp_catalog


def lag_calc(detections, detect_data, template_names, templates,
             shift_len=0.2, min_cc=0.4):
    """
    Main lag-calculation function for detections of specific events.

    Overseer function to take a list of detection objects, cut the data for
    them to lengths of the same length of the template + shift_len on
    either side. This will then write out SEISAN s-file or QuakeML for the
    detections with pick times based on the lag-times found at the maximum
    correlation, providing that correlation is above the min_cc.

    :type detections: list
    :param detections: List of DETECTION objects
    :type detect_data: obspy.core.stream.Stream
    :param detect_data: All the data needed to cut from - can be a gappy Stream
    :type template_names: list
    :param template_names: List of the template names, used to help identify \
        families of events. Must be in the same order as templates.
    :type templates: list
    :param templates: List of the templates, templates are of type: \
        obspy.core.stream.Stream.
    :type shift_len: float
    :param shift_len: Shift length allowed for the pick in seconds, will be
        plus/minus this amount - default=0.2
    :type min_cc: float
    :param min_cc: Minimum cross-correlation value to be considered a pick,
        default=0.4

    :returns: Catalog of events with picks.  No origin imformation is \
        included, these events can then be written out via \
        obspy.core.event functions, or to seisan Sfiles using Sfile_util \
        and located.
    :rtype: obspy.core.event.Catalog

    .. rubric: Example

    >>> from eqcorrscan.core import lag_calc

    .. note:: Picks output in catalog are generated relative to the template \
        start-time.  For example, if you generated your template with a \
        pre_pick time of 0.2 seconds, you should expect picks generated by \
        lag_calc to occur 0.2 seconds before the true phase-pick.  This \
        is because we do not currently store template meta-data alongside the \
        templates.

    .. warning:: Because of the above note, origin times will be consistently \
        shifted by the static pre_pick applied to the templates.
    """
    from obspy import Stream
    from obspy.core.event import Catalog

    # Establish plugin directory relative to this module

    # First work out the delays for each template
    delays = []  # List of tuples of (tempname, (sta, chan, delay))
    zipped_templates = zip(template_names, templates)
    for template in zipped_templates:
        temp_delays = []
        for tr in template[1]:
            temp_delays.append((tr.stats.station, tr.stats.channel,
                                tr.stats.starttime - template[1].
                                sort(['starttime'])[0].stats.starttime))
        delays.append((template[0], temp_delays))
    # List of tuples of (template name, Stream()) for each detection
    detect_streams = []
    for detection in detections:
        # Stream to be saved for new detection
        detect_stream = []
        for tr in detect_data:
            tr_copy = tr.copy()
            # Right now, copying each trace hundreds of times...
            template = [t for t in zipped_templates if t[0] == detection.
                        template_name][0]
            template = template[1].select(station=tr.stats.station,
                                          channel=tr.stats.channel)
            if template:
                # Save template trace length in seconds
                template_len = len(template[0]) / \
                    template[0].stats.sampling_rate
            else:
                continue
                # If there is no template-data match then skip the rest
                # of the trace loop.
            # Grab the delays for the desired template: [(sta, chan, delay)]
            delay = [delay for delay in delays if delay[0] == detection.
                     template_name][0][1]
            # Now grab the delay for the desired trace for this template
            delay = [d for d in delay if d[0] == tr.stats.station and
                     d[1] == tr.stats.channel][0][2]
            detect_stream.append(tr_copy.trim(starttime=detection.detect_time -
                                              shift_len + delay,
                                              endtime=detection.detect_time +
                                              delay + shift_len +
                                              template_len))
            del tr_copy
        # Create tuple of (template name, data stream)
        detect_streams.append((detection.template_name, Stream(detect_stream)))
    # Segregate detections by template, then feed to day_loop
    initial_cat = Catalog()
    for template in zipped_templates:
        template_detections = [detect[1] for detect in detect_streams
                               if detect[0] == template[0]]
        if len(template_detections) > 0:  # Way to remove this?
            initial_cat += _day_loop(template_detections, template[1], min_cc)
    return initial_cat


if __name__ == '__main__':
    import doctest
    doctest.testmod()
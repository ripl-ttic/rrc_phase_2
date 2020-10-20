#!/bin/bash
image=$1
logdir=$2
$1 rosrun trifinger_object_tracking tricamera_log_converter.py ${logdir}/camera_data.dat ${logdir}/video60.avi -c camera60
$1 rosrun trifinger_object_tracking tricamera_log_converter.py ${logdir}/camera_data.dat ${logdir}/video180.avi -c camera180
$1 rosrun trifinger_object_tracking tricamera_log_converter.py ${logdir}/camera_data.dat ${logdir}/video300.avi -c camera300

ffmpeg -i ${logdir}/video60.avi -i ${logdir}/video180.avi -filter_complex hstack -q:v 1 ${logdir}/video_temp.avi
ffmpeg -i ${logdir}/video_temp.avi -i ${logdir}/video300.avi -filter_complex hstack -q:v 1 ${logdir}/video.avi

rm ${logdir}/video60.avi
rm ${logdir}/video180.avi
rm ${logdir}/video300.avi
rm ${logdir}/video_temp.avi
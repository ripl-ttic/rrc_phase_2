#!/usr/bin/env python3

import os
import shelve
import argparse
import robot_fingers
import trifinger_simulation
import pybullet as p
import numpy as np
from trifinger_simulation.tasks import move_cube
from trifinger_simulation import camera, visual_objects
import trifinger_object_tracking.py_tricamera_types as tricamera
import trifinger_cameras
from trifinger_cameras.utils import convert_image
from scipy.spatial.transform import Rotation as R
import cv2
import json


def load_data(path):
    data = {}
    with shelve.open(path) as f:
        for key, val in f.items():
            data[key] = val
    return data


# Mostly copied from move_cube.py
def evaluate_state(goal_pose, actual_pose, difficulty):
    """Compute cost of a given cube pose.  Less is better.

    Args:
        goal:  Goal pose dict of the cube.
        actual_pose:  Actual pose of the cube.
        difficulty:  The difficulty level of the goal (see
            :func:`sample_goal`).  The metric for evaluating a state differs
            depending on the level.

    Returns:
        Cost of the actual pose w.r.t. to the goal pose.  Lower value means
        that the actual pose is closer to the goal.  Zero if actual == goal.
    """
    _CUBE_WIDTH = 0.065
    _ARENA_RADIUS = 0.195

    _cube_3d_radius = _CUBE_WIDTH * np.sqrt(3) / 2
    _max_cube_com_distance_to_center = _ARENA_RADIUS - _cube_3d_radius

    _min_height = _CUBE_WIDTH / 2
    _max_height = 0.1

    def weighted_position_error():
        range_xy_dist = _ARENA_RADIUS * 2
        range_z_dist = _max_height

        xy_dist = np.linalg.norm(
            goal_pose['position'][:2] - actual_pose.position[:2]
        )
        z_dist = abs(goal_pose['position'][2] - actual_pose.position[2])

        # weight xy- and z-parts by their expected range
        return (xy_dist / range_xy_dist + z_dist / range_z_dist) / 2

    if difficulty in (1, 2, 3):
        # consider only 3d position
        return weighted_position_error()
    elif difficulty == 4:
        # consider whole pose
        scaled_position_error = weighted_position_error()

        # https://stackoverflow.com/a/21905553
        goal_rot = R.from_quat(goal_pose['orientation'])
        actual_rot = R.from_quat(actual_pose.orientation)
        error_rot = goal_rot.inv() * actual_rot
        orientation_error = error_rot.magnitude()

        # scale both position and orientation error to be within [0, 1] for
        # their expected ranges
        scaled_orientation_error = orientation_error / np.pi

        scaled_error = (scaled_position_error + scaled_orientation_error) / 2
        return scaled_error

        # Use DISP distance (max. displacement of the corners)
        # goal_corners = get_cube_corner_positions(goal_pose)
        # actual_corners = get_cube_corner_positions(actual_pose)
        # disp = max(np.linalg.norm(goal_corners - actual_corners, axis=1))
    else:
        raise ValueError("Invalid difficulty %d" % difficulty)


class SphereMarker:
    def __init__(self, radius, position, color=(0, 1, 0, 0.5)):
        """
        Create a sphere marker for visualization

        Args:
            width (float): Length of one side of the cube.
            position: Position (x, y, z)
            orientation: Orientation as quaternion (x, y, z, w)
            color: Color of the cube as a tuple (r, b, g, q)
            """
        self.shape_id = p.createVisualShape(
            shapeType=p.GEOM_SPHERE,
            radius=radius,
            rgbaColor=color,
        )
        self.body_id = p.createMultiBody(
            baseVisualShapeIndex=self.shape_id,
            basePosition=position,
            baseOrientation=[0, 0, 0, 1],
        )

    def set_state(self, position):
        """Set pose of the marker.

        Args:
            position: Position (x, y, z)
        """
        orientation = [0, 0, 0, 1]
        p.resetBasePositionAndOrientation(
            self.body_id, position, orientation
        )

    def __del__(self):
        """
        Removes the visual object from the environment
        """
        # At this point it may be that pybullet was already shut down. To avoid
        # an error, only remove the object if the simulation is still running.
        if p.isConnected():
            p.removeBody(self.body_id)


class VisualCubeOrientation:
    '''visualize cube orientation by three cylinder'''
    def __init__(self, cube_position, cube_orientation, cube_halfwidth=0.0325):
        self.markers = []
        self.cube_halfwidth = cube_halfwidth

        color_cycle = [[1, 0, 0, 1], [0, 1, 0, 1], [0, 0, 1, 1]]

        self.z_axis = np.asarray([0,0,1])

        const = 1 / np.sqrt(2)
        x_rot = R.from_quat([const, 0, const, 0])
        y_rot = R.from_quat([0, const, const, 0])
        z_rot = R.from_quat([0,0,0,1])

        assert( np.linalg.norm( x_rot.apply(self.z_axis) - np.asarray([1., 0., 0.]) ) < 0.00000001)
        assert( np.linalg.norm( y_rot.apply(self.z_axis) - np.asarray([0., 1., 0.]) ) < 0.00000001)
        assert( np.linalg.norm( z_rot.apply(self.z_axis) - np.asarray([0., 0., 1.]) ) < 0.00000001)

        self.rotations = [x_rot, y_rot, z_rot]
        cube_rot = R.from_quat(cube_orientation)

        #x: red , y: green, z: blue
        for rot, color in zip(self.rotations, color_cycle):
            rotation = cube_rot * rot
            orientation = rotation.as_quat()
            bias = rotation.apply(self.z_axis) * cube_halfwidth
            self.markers.append(
                CylinderMarker(radius=cube_halfwidth/20,
                               length=cube_halfwidth*2,
                               position=cube_position + bias,
                               orientation=orientation,
                               color=color)
            )

    def set_state(self, position, orientation):
        cube_rot = R.from_quat(orientation)
        for rot, marker in zip(self.rotations, self.markers):
            rotation = cube_rot * rot
            orientation = rotation.as_quat()
            bias = rotation.apply(self.z_axis) * self.cube_halfwidth
            marker.set_state(position=position + bias,
                             orientation=orientation)


class CylinderMarker:
    """Visualize a cylinder."""

    def __init__(
        self, radius, length, position, orientation, color=(0, 1, 0, 0.5)):
        """
        Create a cylinder marker for visualization

        Args:
            radius (float): radius of cylinder.
            length (float): length of cylinder.
            position: Position (x, y, z)
            orientation: Orientation as quaternion (x, y, z, w)
            color: Color of the cube as a tuple (r, b, g, q)
        """

        self.shape_id = p.createVisualShape(
            shapeType=p.GEOM_CYLINDER,
            radius=radius,
            length=length,
            rgbaColor=color
        )
        self.body_id = p.createMultiBody(
            baseVisualShapeIndex=self.shape_id,
            basePosition=position,
            baseOrientation=orientation
        )

    def set_state(self, position, orientation):
        """Set pose of the marker.

        Args:
            position: Position (x, y, z)
            orientation: Orientation as quaternion (x, y, z, w)
        """
        p.resetBasePositionAndOrientation(
            self.body_id,
            position,
            orientation
        )


class CubeDrawer:
    def __init__(self, logdir):
        calib_files = []
        for name in ("camera60", "camera180", "camera300"):
            calib_files.append(os.path.join(logdir, name + ".yml"))
        self.cube_visualizer = tricamera.CubeVisualizer(calib_files)

    def add_cube(self, images, object_pose):
        cvmats = [trifinger_cameras.camera.cvMat(img) for img in images]
        images = self.cube_visualizer.draw_cube(cvmats, object_pose, False)
        images = [np.array(img) for img in images]

        images = [cv2.putText(
            image,
            "confidence: %.2f" % object_pose.confidence,
            (0, image.shape[0] - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 255, 0)
        ) for image in images]
        return images


class VideoRecorder:
    def __init__(self, fps, image_size=(270, 270)):
        self.fps = fps
        self.image_size = image_size
        self.frame_size = None
        self.cameras = camera.TriFingerCameras(image_size=image_size)
        self.frames = []

    def get_views(self):
        images = [self.cameras.cameras[i].get_image(renderer=p.ER_TINY_RENDERER) for i in range(3)]
        three_views = np.concatenate((*images,), axis=1)
        return three_views

    def capture_frame(self):
        three_views = self.get_views()
        self.add_frame(three_views)
        return three_views

    def add_frame(self, frame):
        if self.frame_size is None:
            self.frame_size = frame.shape[:2]
        assert frame.shape[:2] == self.frame_size
        self.frames.append(frame)

    def save_video(self, filepath):
        out = cv2.VideoWriter(filepath, cv2.VideoWriter_fourcc(*'XVID'),
                              self.fps, (self.frame_size[1], self.frame_size[0]))
        for frame in self.frames:
            out.write(frame)
        out.release()


def get_synced_log_data(logdir, goal, difficulty):
    log = robot_fingers.TriFingerPlatformLog(os.path.join(logdir, "robot_data.dat"),
                                             os.path.join(logdir, "camera_data.dat"))
    log_camera = tricamera.LogReader(os.path.join(logdir, "camera_data.dat"))
    stamps = log_camera.timestamps

    obs = {'robot': [], 'cube': [], 'images': [], 't': [], 'desired_action': [],
           'stamp': [], 'acc_reward': []}
    ind = 0
    acc_reward = 0.0
    for t in range(log.get_first_timeindex(), log.get_last_timeindex() + 1):
        camera_observation = log.get_camera_observation(t)
        acc_reward -= evaluate_state(
            goal, camera_observation.filtered_object_pose, difficulty
        )
        if 1000 * log.get_timestamp_ms(t) >= stamps[ind]:
            robot_observation = log.get_robot_observation(t)
            obs['robot'].append(robot_observation)
            obs['cube'].append(camera_observation.filtered_object_pose)
            obs['images'].append([convert_image(camera.image)
                                  for camera in camera_observation.cameras])
            obs['desired_action'].append(log.get_desired_action(t))
            obs['acc_reward'].append(acc_reward)
            obs['t'].append(t)
            obs['stamp'].append(log.get_timestamp_ms(t))
            ind += 1
    return obs


def get_goal(logdir):
    filename = os.path.join(logdir, 'goal.json')
    with open(filename, 'r') as f:
        goal = json.load(f)
    return goal['goal'], goal['difficulty']


def add_text(frame, text, position, **kwargs):
    frame = cv2.putText(frame, text, position,
                        fontFace=cv2.FONT_HERSHEY_SIMPLEX, fontScale=0.7, color=(0, 0, 0),
                        thickness=2, lineType=cv2.LINE_AA, **kwargs)
    return frame


def main(logdir, video_path):
    custom_log = load_data(os.path.join(logdir, 'user/custom_data'))
    goal, difficulty = get_goal(logdir)
    data = get_synced_log_data(logdir, goal, difficulty)
    fps = len(data['t']) / (data['stamp'][-1] - data['stamp'][0])
    video_recorder = VideoRecorder(fps)
    cube_drawer = CubeDrawer(logdir)

    initial_object_pose = move_cube.Pose(data['cube'][0].position,
                                         data['cube'][0].orientation)
    platform = trifinger_simulation.TriFingerPlatform(
        visualization=False,
        initial_object_pose=initial_object_pose,
    )
    markers = []
    marker_cube_ori = VisualCubeOrientation(data['cube'][0].position,
                                            data['cube'][0].orientation)
    marker_goal_ori = VisualCubeOrientation(goal['position'], goal['orientation'])

    visual_objects.CubeMarker(
        width=0.065,
        position=goal['position'],
        orientation=goal['orientation'],
        physicsClientId=platform.simfinger._pybullet_client_id,
    )
    # if 'grasp_target_cube_pose' in custom_log:
    #     markers.append(
    #         visual_objects.CubeMarker(
    #             width=0.065,
    #             position=custom_log['grasp_target_cube_pose']['position'],
    #             orientation=custom_log['grasp_target_cube_pose']['orientation'],
    #             color=(0, 0, 1, 0.5),
    #             physicsClientId=platform.simfinger._pybullet_client_id,
    #         )
    #     )
    if 'pregrasp_tip_positions' in custom_log:
        for tip_pos in custom_log['pregrasp_tip_positions']:
            print(tip_pos)
            markers.append(
                SphereMarker(
                    radius=0.015,
                    position=tip_pos,
                    color=(0, 1, 1, 0.5)
                )
            )
    if 'failure_target_tip_positions' in custom_log:
        for tip_pos in custom_log['failure_target_tip_positions']:
            print(tip_pos)
            markers.append(
                SphereMarker(
                    radius=0.015,
                    position=tip_pos,
                    color=(1, 0, 0, 0.5)
                )
            )
    if 'pitch_grasp_positions' in custom_log:
        for tip_pos in custom_log['pitch_grasp_positions']:
            print(tip_pos)
            markers.append(
                SphereMarker(
                    radius=0.015,
                    position=tip_pos,
                    color=(1, 1, 1, 0.5)
                )
            )
    if 'yaw_grasp_positions' in custom_log:
        for tip_pos in custom_log['yaw_grasp_positions']:
            print(tip_pos)
            markers.append(
                SphereMarker(
                    radius=0.015,
                    position=tip_pos,
                    color=(0, 0, 0, 0.5)
                )
            )

    p.configureDebugVisualizer(p.COV_ENABLE_GUI, 0)
    p.resetDebugVisualizerCamera(cameraDistance=0.6, cameraYaw=0, cameraPitch=-40, cameraTargetPosition=[0,0,0])

    for i, t in enumerate(data['t']):
        platform.simfinger.reset_finger_positions_and_velocities(data['desired_action'][i].position)
        platform.cube.set_state(data['cube'][i].position, data['cube'][i].orientation)
        marker_cube_ori.set_state(data['cube'][i].position, data['cube'][i].orientation)
        frame_desired = video_recorder.get_views()
        frame_desired = cv2.cvtColor(frame_desired, cv2.COLOR_RGB2BGR)
        platform.simfinger.reset_finger_positions_and_velocities(data['robot'][i].position)
        frame_observed = video_recorder.get_views()
        frame_observed = cv2.cvtColor(frame_observed, cv2.COLOR_RGB2BGR)
        frame_real = np.concatenate(data['images'][i], axis=1)
        frame_real_cube = np.concatenate(cube_drawer.add_cube(data['images'][i],
                                                              data['cube'][i]),
                                         axis=1)

        frame = np.concatenate((frame_desired, frame_observed,
                                frame_real, frame_real_cube), axis=0)
        # add text
        frame = add_text(frame, text='step: {:06d}'.format(t), position=(10, 40))
        frame = add_text(frame, text='acc reward: {:.3f}'.format(data['acc_reward'][i]), position=(10, 70))
        video_recorder.add_frame(frame)
    video_recorder.save_video(video_path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("logdir", help="path to the log directory")
    parser.add_argument("video_path", help="video file to save (.avi file)")
    args = parser.parse_args()
    main(args.logdir, args.video_path)

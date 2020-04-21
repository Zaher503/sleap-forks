import os
import subprocess as sub
import tempfile
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Text

from PySide2 import QtWidgets

from sleap import Labels, Video
from sleap.gui.learning.configs import ConfigFileInfo
from sleap.nn import training
from sleap.nn.config import TrainingJobConfig

SKIP_TRAINING = False


def write_pipeline_files(
    output_dir: str,
    labels_filename: str,
    config_info_list: List[ConfigFileInfo],
    inference_params: Dict[str, Any],
    frames_to_predict: Dict[Video, List[int]] = None,
):
    """Writes the config files and scripts for manually running pipeline."""

    # Use absolute path for all files that aren't contained in the output dir.
    labels_filename = os.path.abspath(labels_filename)

    video_path_map = {
        video: os.path.abspath(video.filename) for video in frames_to_predict
    }

    # Preserve current working directory and change working directory to the
    # output directory, so we can set local paths relative to that.
    old_cwd = os.getcwd()
    os.chdir(output_dir)

    new_cfg_filenames = []
    train_script = "#!/bin/bash\n"

    for cfg_info in config_info_list:
        if cfg_info.dont_retrain:
            # Use full absolute path to already training model
            trained_path = os.path.normpath(os.path.join(old_cwd, cfg_info.path))
            new_cfg_filenames.append(trained_path)

        else:
            # We're training this model, so save config file...

            # First we want to set the run folder so that we know where to find
            # the model after it's trained.
            # We'll use local path to the output directory (cwd).
            # Note that setup_new_run_folder does things relative to cwd which
            # is the main reason we're setting it to the output directory rather
            # than just using normpath.
            cfg_info.config.outputs.runs_folder = ""
            training.setup_new_run_folder(cfg_info.config.outputs)

            # Now we set the filename for the training config file
            new_cfg_filename = f"{cfg_info.head_name}.json"

            # Save the config file
            cfg_info.config.save_json(new_cfg_filename)

            # Keep track of the path where we'll find the trained model
            new_cfg_filenames.append(cfg_info.config.outputs.run_name)

            # Add a line to the script for training this model
            train_script += f"sleap-train {new_cfg_filename} {labels_filename}\n"

    # Write the script to train the models which need to be trained
    with open(os.path.join(output_dir, "train-script.sh"), "w") as f:
        f.write(train_script)

    # Build the script for running inference
    inference_script = "#!/bin/bash\n"
    for video, video_frames in frames_to_predict.items():
        # We want to save predictions in output dir so use local path
        prediction_output_path = f"{os.path.basename(video.filename)}.predictions.slp"

        # Get list of cli args
        cli_args, _ = make_predict_cli_call(
            video=video,
            video_path=video_path_map[video],
            trained_job_paths=new_cfg_filenames,
            kwargs=inference_params,
            frames=video_frames,
            labels_filename=labels_filename,
            output_path=prediction_output_path,
        )
        # And join them into a single call to inference
        inference_script += " ".join(cli_args) + "\n"

    # And write it
    with open(os.path.join(output_dir, "inference-script.sh"), "w") as f:
        f.write(inference_script)

    # Restore the working directory
    os.chdir(old_cwd)


def run_learning_pipeline(
    labels_filename: str,
    labels: Labels,
    config_info_list: List[ConfigFileInfo],
    inference_params: Dict[str, Any],
    frames_to_predict: Dict[Video, List[int]] = None,
) -> int:
    """Runs training (as needed) and inference.

    Args:
        labels_filename: Path to already saved current labels object.
        labels: The current labels object; results will be added to this.
        config_info_list: List of ConfigFileInfo with configs for training
            and inference.
        inference_params: Parameters to pass to inference.
        frames_to_predict: Dict that gives list of frame indices for each video.

    Returns:
        Number of new frames added to labels.

    """

    save_viz = inference_params.get("_save_viz", False)

    # Train the TrainingJobs
    trained_job_paths = run_gui_training(
        labels_filename=labels_filename,
        labels=labels,
        config_info_list=config_info_list,
        gui=True,
        save_viz=save_viz,
    )

    # Check that all the models were trained
    if None in trained_job_paths.values():
        return -1

    trained_job_paths = list(trained_job_paths.values())

    # Run the Predictor for suggested frames
    new_labeled_frame_count = run_gui_inference(
        labels=labels,
        trained_job_paths=trained_job_paths,
        inference_params=inference_params,
        frames_to_predict=frames_to_predict,
        labels_filename=labels_filename,
    )

    return new_labeled_frame_count


def run_gui_training(
    labels_filename: str,
    labels: Labels,
    config_info_list: List[ConfigFileInfo],
    gui: bool = True,
    save_viz: bool = False,
) -> Dict[Text, Text]:
    """
    Runs training for each training job.

    Args:
        labels: Labels object from which we'll get training data.
        config_info_list: List of ConfigFileInfo with configs for training.
        gui: Whether to show gui windows and process gui events.
        save_viz: Whether to save visualizations from training.

    Returns:
        Dictionary, keys are head name, values are path to trained config.
    """

    trained_job_paths = dict()

    if gui:
        from sleap.nn.monitor import LossViewer
        from sleap.gui.widgets.imagedir import QtImageDirectoryWidget

        # open training monitor window
        win = LossViewer()
        win.resize(600, 400)
        win.show()

    for config_info in config_info_list:
        if config_info.dont_retrain:

            if not config_info.has_trained_model:
                raise ValueError(
                    f"Config is set to not retrain but no trained model found: {config_info.path}"
                )

            print(
                f"Using already trained model for {config_info.head_name}: {config_info.path}"
            )

            trained_job_paths[config_info.head_name] = config_info.path

        else:
            job = config_info.config
            model_type = config_info.head_name

            # Update save dir and run name for job we're about to train
            # so we have access to them here (rather than letting
            # train_subprocess update them).
            # training.Trainer.set_run_name(job, labels_filename)
            job.outputs.runs_folder = os.path.join(
                os.path.dirname(labels_filename), "models"
            )
            training.setup_new_run_folder(
                job.outputs, base_run_name=f"{model_type}.{len(labels)}"
            )

            if gui:
                print("Resetting monitor window.")
                win.reset(what=str(model_type))
                win.setWindowTitle(f"Training Model - {str(model_type)}")
                win.set_message(f"Preparing to run training...")
                if save_viz:
                    viz_window = QtImageDirectoryWidget.make_training_vizualizer(
                        job.outputs.run_path
                    )
                    viz_window.move(win.x() + win.width() + 20, win.y())
                    win.on_epoch.connect(viz_window.poll)

            print(f"Start training {str(model_type)}...")

            def waiting():
                if gui:
                    QtWidgets.QApplication.instance().processEvents()

            # Run training
            trained_job_path, success = train_subprocess(
                job, labels_filename, waiting_callback=waiting, save_viz=save_viz,
            )

            if success:
                # get the path to the resulting TrainingJob file
                trained_job_paths[model_type] = trained_job_path
                print(f"Finished training {str(model_type)}.")
            else:
                if gui:
                    win.close()
                    QtWidgets.QMessageBox(
                        text=f"An error occurred while training {str(model_type)}. Your command line terminal may have more information about the error."
                    ).exec_()
                trained_job_paths[model_type] = None

    if gui:
        # close training monitor window
        win.close()

    return trained_job_paths


def run_gui_inference(
    labels: Labels,
    trained_job_paths: List[str],
    frames_to_predict: Dict[Video, List[int]],
    inference_params: Dict[str, str],
    labels_filename: str,
    gui: bool = True,
) -> int:
    """Run inference on specified frames using models from training_jobs.

    Args:
        labels: The current labels object; results will be added to this.
        trained_job_paths: List of paths to TrainingJobs with trained models.
        frames_to_predict: Dict that gives list of frame indices for each video.
        inference_params: Parameters to pass to inference.
        labels_filename: Path to labels dataset
        gui: Whether to show gui windows and process gui events.

    Returns:
        Number of new frames added to labels.
    """

    if gui:
        # show message while running inference
        progress = QtWidgets.QProgressDialog(
            f"Running inference on {len(frames_to_predict)} videos...",
            "Cancel",
            0,
            len(frames_to_predict),
        )
        progress.show()
        QtWidgets.QApplication.instance().processEvents()

    new_lfs = []
    for i, (video, frames) in enumerate(frames_to_predict.items()):

        if len(frames):

            def waiting():
                if gui:
                    QtWidgets.QApplication.instance().processEvents()
                    progress.setValue(i)
                    if progress.wasCanceled():
                        return -1

            # Run inference for desired frames in this video
            predictions_path, success = predict_subprocess(
                video=video,
                frames=frames,
                trained_job_paths=trained_job_paths,
                kwargs=inference_params,
                waiting_callback=waiting,
                labels_filename=labels_filename,
            )

            if success:
                predictions_labels = Labels.load_file(predictions_path, match_to=labels)
                new_lfs.extend(predictions_labels.labeled_frames)
            else:
                if gui:
                    progress.close()
                    QtWidgets.QMessageBox(
                        text=f"An error occcured during inference. Your command line terminal may have more information about the error."
                    ).exec_()
                return -1

    # Remove any frames without instances
    new_lfs = list(filter(lambda lf: len(lf.instances), new_lfs))

    # Merge predictions into current labels dataset
    _, _, new_conflicts = Labels.complex_merge_between(
        labels,
        new_labels=Labels(new_lfs),
        unify=False,  # since we used match_to when loading predictions file
    )

    # new predictions should replace old ones
    Labels.finish_complex_merge(labels, new_conflicts)

    # close message window
    if gui:
        progress.close()

    # return total_new_lf_count
    return len(new_lfs)


def train_subprocess(
    job_config: TrainingJobConfig,
    labels_filename: str,
    waiting_callback: Optional[Callable] = None,
    save_viz: bool = False,
):
    """Runs training inside subprocess."""

    # run_name = job_config.outputs.run_name
    run_path = job_config.outputs.run_path

    success = False

    with tempfile.TemporaryDirectory() as temp_dir:

        # Write a temporary file of the TrainingJob so that we can respect
        # any changed made to the job attributes after it was loaded.
        temp_filename = datetime.now().strftime("%y%m%d_%H%M%S") + "_training_job.json"
        training_job_path = os.path.join(temp_dir, temp_filename)
        job_config.save_json(training_job_path)

        # Build CLI arguments for training
        cli_args = [
            "sleap-train",
            training_job_path,
            labels_filename,
            "--zmq",
            # "--run_name",
            # run_name,
        ]

        if save_viz:
            cli_args.append("--save_viz")

        # Use cli arg since cli ignores setting in config
        if job_config.outputs.tensorboard.write_logs:
            cli_args.append("--tensorboard")

        print(cli_args)

        if not SKIP_TRAINING:
            # Run training in a subprocess
            with sub.Popen(cli_args) as proc:

                # Wait till training is done, calling a callback if given.
                while proc.poll() is None:
                    if waiting_callback is not None:
                        if waiting_callback() == -1:
                            # -1 signals user cancellation
                            return "", False
                    time.sleep(0.1)

                success = proc.returncode == 0

    print("Run Path:", run_path)

    return run_path, success


def make_predict_cli_call(
    video: "Video",
    trained_job_paths: List[str],
    kwargs: Dict[str, str],
    frames: Optional[List[int]] = None,
    labels_filename: Optional[str] = None,
    video_path: Optional[str] = None,
    output_path: Optional[str] = None,
):
    cli_args = ["sleap-track"]

    if not trained_job_paths and "tracking.tracker" in kwargs and labels_filename:
        # No models so we must want to re-track previous predictions
        cli_args.append(labels_filename)
    elif video_path is not None:
        cli_args.append(video_path)
    else:
        cli_args.append(video.filename)

    # TODO: better support for video params
    if hasattr(video.backend, "dataset") and video.backend.dataset:
        cli_args.extend(("--video.dataset", video.backend.dataset))

    if hasattr(video.backend, "input_format") and video.backend.input_format:
        cli_args.extend(("--video.input_format", video.backend.input_format))

    # Make path where we'll save predictions (if not specified)
    if output_path is None:

        if labels_filename:
            # Make a predictions directory next to the labels dataset file
            predictions_dir = os.path.join(
                os.path.dirname(labels_filename), "predictions"
            )
            os.makedirs(predictions_dir, exist_ok=True)
        else:
            # Dataset filename wasn't given, so save predictions in same dir
            # as the video
            predictions_dir = os.path.dirname(video.filename)

        # Build filename with video name and timestamp
        timestamp = datetime.now().strftime("%y%m%d_%H%M%S")
        output_path = os.path.join(
            predictions_dir,
            f"{os.path.basename(video.filename)}.{timestamp}.predictions.slp",
        )

    for job_path in trained_job_paths:
        cli_args.extend(("-m", job_path))

    for key, val in kwargs.items():
        if not key.startswith(("_", "outputs.", "model.")):
            cli_args.extend((f"--{key}", str(val)))

    cli_args.extend(("--frames", ",".join(map(str, frames))))

    cli_args.extend(("-o", output_path))

    return cli_args, output_path


def predict_subprocess(
    video: "Video",
    trained_job_paths: List[str],
    kwargs: Dict[str, str],
    frames: Optional[List[int]] = None,
    waiting_callback: Optional[Callable] = None,
    labels_filename: Optional[str] = None,
):
    cli_args, output_path = make_predict_cli_call(
        video, trained_job_paths, kwargs, frames, labels_filename
    )

    print("Command line call:")
    print(" \\\n".join(cli_args))
    print()

    with sub.Popen(cli_args) as proc:
        while proc.poll() is None:
            if waiting_callback is not None:

                if waiting_callback() == -1:
                    # -1 signals user cancellation
                    return "", False

            time.sleep(0.1)

        print(f"Process return code: {proc.returncode}")
        success = proc.returncode == 0

    return output_path, success
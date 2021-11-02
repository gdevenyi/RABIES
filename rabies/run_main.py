import os
import sys
import pickle
import logging
import argparse
from pathlib import Path
import pathos.multiprocessing as multiprocessing  # Better multiprocessing
import SimpleITK as sitk
from .boilerplate import *

if 'XDG_DATA_HOME' in os.environ.keys():
    rabies_path = os.environ['XDG_DATA_HOME']+'/rabies'
else:
    rabies_path = os.environ['HOME']+'/.local/share/rabies'


def get_parser():
    """Build parser object"""
    parser = argparse.ArgumentParser(
        description="""RABIES performs processing of rodent fMRI images. Can either run
        on datasets that only contain EPI images, or both structural and EPI images.""",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    subparsers = parser.add_subparsers(title='Commands',
                                       description="""The RABIES workflow is seperated into three different processing steps: preprocessing,
                                                   confound regression and analysis. Outputs from the preprocessing provides the inputs for
                                                   the subsequent confound regression, and finally analysis.""",
                                       help='Description',
                                       dest='rabies_step',
                                       metavar='Processing step')

    preprocess = subparsers.add_parser("preprocess",
                                       help="""
        Conducts preprocessing on an input dataset in BIDS format.
        Preprocessing includes realignment for motion, correction for susceptibility distortions through non-linear registration,
        registration to a commonspace atlas and associated masks, evaluation of confounding timecourses, and includes various
        execution options (see --help).
        """, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    confound_correction = subparsers.add_parser("confound_correction",
                                                help="""
        Flexible options for confound regression are available to apply directly on preprocessing outputs from RABIES.
        After linear detrending, only selected options are applied sequentially in following the order: 1) ICA-AROMA,
        2) highpass/lowpass filtering, 3) frame censoring (from FD/DVARS measures), 4) regression of confound timeseries,
        5) standardization of timeseries, and 6) spatial smoothing.
        """, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    analysis = subparsers.add_parser("analysis",
                                     help="""
        A few built-in resting-state functional connectivity (FC) analysis options are provided to conduct rapid analysis
        on the cleaned timeseries.
        Options include seed-based FC, whole-brain correlation FC matrix, group-ICA, dual regression and dual ICA.
        """, formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    g_execution = parser.add_argument_group(title='Execution Options', description="""
        Options for parallel execution of the workflow and memory management.
        WARNING: CHANGING EXECUTION OPTIONS ON A PREVIOUS EXECUTION RISKS
        RE-STARTING THE PROCESSING OF STEPS PREVIOUSLY RUN
        """)
    g_execution.add_argument("-p", "--plugin", default='Linear',
                             choices=['Linear', 'MultiProc', 'SGE', 'SGEGraph',
                                      'PBS', 'LSF', 'SLURM', 'SLURMGraph'],
                             help="""
                             Specify the nipype plugin for workflow execution.
                             Consult https://nipype.readthedocs.io/en/0.11.0/users/plugins.html for details.
                             """)
    g_execution.add_argument('--local_threads', type=int, default=multiprocessing.cpu_count(),
                             help="""
                             For local MultiProc execution, set the maximum number of processors run in parallel,
                             defaults to number of CPUs.
                             """)
    g_execution.add_argument("--scale_min_memory", type=float, default=1.0,
                             help="""
                             For a parallel execution with MultiProc, the minimal memory attributed to nodes can
                             be scaled with this multiplier to avoid memory crashes.
                             """)
    g_execution.add_argument("--min_proc", type=int, default=1,
                             help="""
                             For SGE parallel processing, specify the minimal number of nodes to be assigned to
                             avoid memory crashes.
                             """)

    preprocess.add_argument('bids_dir', action='store', type=Path,
                            help="""
                            the root folder of the BIDS-formated input data directory.
                            """)
    preprocess.add_argument('output_dir', action='store', type=Path,
                            help="""
                            the output path to drop outputs from major preprocessing steps.
                            """)
    preprocess.add_argument("--bold_only", dest='bold_only', action='store_true',
                            help="""
                            Apply preprocessing with only EPI scans. Commonspace registration
                            is executed directly using the corrected EPI 3D reference images.
                            The commonspace registration simultaneously applies distortion
                            correction, this option will produce only commonspace outputs.
                            """)
    preprocess.add_argument('--anat_autobox', dest='anat_autobox', action='store_true',
                            help="""
                            Crops out extra space around the brain on the structural image using AFNI's 3dAutobox https://afni.nimh.nih.gov/pub/dist/doc/program_help/3dAutobox.html.
                            """)
    preprocess.add_argument('--bold_autobox', dest='bold_autobox', action='store_true',
                            help="""
                            Crops out extra space around the brain on the EPI image using AFNI's 3dAutobox https://afni.nimh.nih.gov/pub/dist/doc/program_help/3dAutobox.html.
                            """)
    preprocess.add_argument('--apply_despiking', dest='apply_despiking', action='store_true',
                            help="""
                            Applies AFNI's 3dDespike https://afni.nimh.nih.gov/pub/dist/doc/program_help/3dDespike.html.
                            """)
    preprocess.add_argument("--HMC_option", type=str, default='intraSubjectBOLD',
                            choices=['intraSubjectBOLD', '0', '1', '2', '3'],
                            help="""
                            Select an option for head motion realignment among the pre-built options
                            from https://github.com/ANTsX/ANTsR/blob/master/R/ants_motion_estimation.R.
                            """)
    preprocess.add_argument('--apply_slice_mc', dest='apply_slice_mc', action='store_true',
                            help="""
                            Whether to apply a slice-specific motion correction after initial volumetric HMC.
                            This can correct for interslice misalignment resulting from within-TR motion.
                            With this option, motion corrections and the subsequent resampling from registration are applied
                            sequentially, since the 2D slice registrations cannot be concatenate with 3D transforms.
                            """)
    preprocess.add_argument('--detect_dummy', dest='detect_dummy', action='store_true',
                            help="""
                            Detect and remove initial dummy volumes from the EPI, and generate
                            a reference EPI based on these volumes if detected.
                            Dummy volumes will be removed from the output preprocessed EPI.
                            """)
    preprocess.add_argument("--data_type", type=str, default='float32',
                            choices=['int16', 'int32', 'float32', 'float64'],
                            help="Specify data format outputs to control for file size.")
    preprocess.add_argument("--debug", dest='debug', action='store_true',
                            help="Run in debug mode.")

    g_registration = preprocess.add_argument_group(title='Registration Options', description="""
        Customize options for various registration steps. The three in-built registration script options provided
        consist of a 'Rigid', 'Affine' and 'SyN' (non-linear) registration. Other options also allow alternative
        workflows to troubleshoot registration failures.
        """)
    g_registration.add_argument("--bold_inho_cor_method", type=str, default='Rigid',
                            choices=['Rigid', 'Affine',
                                     'SyN', 'no_reg', 'N4_reg', 'disable'],
                            help="""
                            Select a registration type for masking during inhomogeneity correction of the EPI.
                            """)
    g_registration.add_argument("--bold_inho_cor_otsu", type=int, default=2,
                            help="""
                            """)
    g_registration.add_argument("--anat_inho_cor_method", type=str, default='SyN',
                            choices=['Rigid', 'Affine',
                                     'SyN', 'no_reg', 'N4_reg', 'disable'],
                            help="""
                            Select a registration type for masking during inhomogeneity correction of the structural image.
                            """)
    g_registration.add_argument(
        '--atlas_reg_script',
        type=str,
        default='SyN',
        choices=['Rigid', 'Affine', 'SyN', 'NULL'],
        help="""
        Specify a registration script for alignment of the unbiased dataset template to the atlas.
        """)
    g_registration.add_argument("--fast_commonspace", dest='fast_commonspace', action='store_true',
                                help="""
                                This option will skip the generation of a dataset template from https://github.com/CoBrALab/twolevel_ants_dbm.
                                Instead each anatomical scan will be individually registered to the
                                commonspace template using the --atlas_reg_script.
                                Note that this option, although faster, is expected to reduce the
                                quality of commonspace registration.
                                """)
    g_registration.add_argument("--commonspace_masking", dest='commonspace_masking', action='store_true',
                                help="""
                                If true, will use masks originating from the inhomogeneity correction step
                                to orient commonspace alignment.
                                """)
    g_registration.add_argument("--coreg_script", type=str, default='SyN',
                                choices=['Rigid', 'Affine', 'SyN', 'NULL'],
                                help="""
                                Specify EPI to anat coregistration script.
                                """)
    g_registration.add_argument("--coreg_masking", dest='coreg_masking', action='store_true',
                                help="""
                                If true, will use masks originating from the EPI inhomogeneity correction step
                                to orient alignment to the target anatomical image.
                                """)
    g_registration.add_argument("--brain_extraction", dest='brain_extraction', action='store_true',
                                help="""
                                If using masking during registration from --commonspace_masking/--coreg_masking, can
                                specify to extract the brains using the masks prior to registration, which will 
                                enhance brain edge-matching, but requires good quality masks.
                                """)

    g_resampling = preprocess.add_argument_group(title='Resampling Options', description="""
        The following options allow to customize the voxel dimensions for the preprocessed EPIs or for
        the anatomical images during registration.
        Axis resampling specifications must follow the format 'dim1xdim2xdim3' (in mm) with the RAS axis
        convention (dim1=Right-Left, dim2=Anterior-Posterior, dim3=Superior-Inferior).
        The original dimensions are conserved if 'inputs_defined' is specified.
        """)
    g_resampling.add_argument('--nativespace_resampling', type=str, default='inputs_defined',
                              help="""
                              Can specify a resampling dimension for the nativespace outputs.
                              """)
    g_resampling.add_argument('--commonspace_resampling', type=str, default='inputs_defined',
                              help="""
                              Can specify a resampling dimension for the commonspace outputs.
                              """)
    g_resampling.add_argument('--anatomical_resampling', type=str, default='inputs_defined',
                              help="""
                              Can specify resampling dimensions for the template files to optimize
                              registration efficiency. By defaults ('inputs_defined'), the resampling
                              dimension is estimated from the input images. The smallest dimension among
                              the anatomical images (EPI images instead if --bold_only is True) defines
                              the isotropic resolution for resampling.
                              """)

    g_stc = preprocess.add_argument_group(title='STC Options', description="""
        Specify Slice Timing Correction (STC) info that is fed to AFNI 3dTshift
        (https://afni.nimh.nih.gov/pub/dist/doc/program_help/3dTshift.html). The STC is applied in the
        anterior-posterior orientation, assuming slices were acquired in this direction.
        """)
    g_stc.add_argument('--TR', type=str, default='auto',
                       help="""
                       Specify repetition time (TR) in seconds. (e.g. --TR 1.2)
                       """)
    g_stc.add_argument('--apply_STC', dest='apply_STC', action='store_true',
                       help="""
                       Select this option to apply the STC step.
                       """)
    g_stc.add_argument('--tpattern', type=str, default='alt',
                       choices=['alt', 'seq'],
                       help="""
                       Specify if interleaved or sequential acquisition. 'alt' for interleaved, 'seq' for sequential.
                       """)

    g_atlas = preprocess.add_argument_group(title='Template Files', description="""
        Specify commonspace template and associated mask/label files.
        A mouse atlas is provided as default https://wiki.mouseimaging.ca/display/MICePub/Mouse+Brain+Atlases.
        """)
    g_atlas.add_argument('--anat_template', action='store', type=Path,
                         default=f"{rabies_path}/DSURQE_40micron_average.nii.gz",
                         help="""
                         Anatomical file for the commonspace template.
                         """)
    g_atlas.add_argument('--brain_mask', action='store', type=Path,
                         default=f"{rabies_path}/DSURQE_40micron_mask.nii.gz",
                         help="""
                         Brain mask for the template.
                         """)
    g_atlas.add_argument('--WM_mask', action='store', type=Path,
                         default=f"{rabies_path}/DSURQE_40micron_eroded_WM_mask.nii.gz",
                         help="""
                         White matter mask for the template.
                         """)
    g_atlas.add_argument('--CSF_mask', action='store', type=Path,
                         default=f"{rabies_path}/DSURQE_40micron_eroded_CSF_mask.nii.gz",
                         help="""
                         CSF mask for the template.
                         """)
    g_atlas.add_argument('--vascular_mask', action='store', type=Path,
                         default=f"{rabies_path}/vascular_mask.nii.gz",
                         help="""
                         Can provide a mask of major blood vessels for computing confound timeseries.
                         The default mask was generated by applying MELODIC ICA and selecting the resulting component
                         mapping onto major veins.
                         """)
    g_atlas.add_argument('--labels', action='store', type=Path,
                         default=f"{rabies_path}/DSURQE_40micron_labels.nii.gz",
                         help="""
                         Atlas file with anatomical labels.
                         """)

    confound_correction.add_argument('preprocess_out', action='store', type=Path,
                                     help="""
                                     path to RABIES preprocessing output directory with the datasinks.
                                     """)
    confound_correction.add_argument('output_dir', action='store', type=Path,
                                     help="""
                                     path to drop confound regression output datasink.
                                     """)
    confound_correction.add_argument('--read_datasink', dest='read_datasink', action='store_true', default=False,
                                     help="""
                                     Choose this option to read directly from preprocessing outputs found in the datasinks
                                     instead of executing an integrated workflow which includes previously run steps.
                                     Using this option, it is assumed that outputs in the datasink folders haven't been modified,
                                     and that preprocessing was properly completed.
                                     """)
    confound_correction.add_argument('--output_name', type=str, default='confound_correction_wf',
                                     help="""
                                     Creates a new output folder to store the workflow of this CR run, to avoid potential
                                     overlaps with previous runs (can be useful if investigating multiple strategies).
                                     """)
    confound_correction.add_argument('--nativespace_analysis', dest='nativespace_analysis', action='store_true',
                                     help="""
                                     Use to specify confound correction and analysis on native space outputs.
                                     """)
    confound_correction.add_argument('--TR', type=str, default='auto',
                                     help="""
                                     Specify repetition time (TR) in seconds. (e.g. --TR 1.2)
                                     """)
    confound_correction.add_argument('--highpass', type=float, default=None,
                                     help="""
                                     Specify highpass filter frequency.
                                     """)
    confound_correction.add_argument('--lowpass', type=float, default=None,
                                     help="""
                                     Specify lowpass filter frequency.
                                     """)
    confound_correction.add_argument('--edge_cutoff', type=float, default=30,
                                     help="""
                                     Specify number of seconds to cut at beginning and end of acquisition if applying 
                                     a frequency filter.
                                     Applying frequency filters generate edge effects at begining and end of the sequence. 
                                     We recommend to cut those timepoints (around 30sec at both end for 0.01Hz highpass.).
                                     """)
    confound_correction.add_argument('--smoothing_filter', type=float, default=None,
                                     help="""
                                     Specify spatial smoothing filter size in mm.
                                     Uses nilearn's function https://nilearn.github.io/modules/generated/nilearn.image.smooth_img.html
                                     """)
    confound_correction.add_argument('--run_aroma', dest='run_aroma', action='store_true', default=False,
                                     help="""
                                     Whether to run ICA-AROMA or not. The classifier implemented within RABIES
                                     is a slightly modified version from the original (Pruim et al. 2015),
                                     with parameters and masks adapted for rodent images.
                                     """)
    confound_correction.add_argument('--aroma_dim', type=int, default=0,
                                     help="""
                                     Can specify a fixed number of dimension for the MELODIC run before ICA-AROMA.
                                     """)
    confound_correction.add_argument('--aroma_random_seed', type=int, default=1,
                             help="""
                             For reproducibility, can manually set a random seed for MELODIC.
                             """)
    confound_correction.add_argument('--conf_list', type=str,
                                     nargs="*",  # 0 or more values expected => creates a list
                                     default=[],
                                     choices=["WM_signal", "CSF_signal", "vascular_signal",
                                              "global_signal", "aCompCor", "mot_6", "mot_24", "mean_FD"],
                                     help="""
                                     List of nuisance regressors that will be applied on voxel timeseries, i.e., confound regression.
                                     WM/CSF/vascular/global_signal: correspond to mean signal from WM/CSF/vascular/brain masks.
                                     mot_6: 6 rigid HMC parameters.
                                     mot_24: mot_6 + their temporal derivative, and all 12 parameters squared (Friston et al. 1996).
                                     aCompCor: corresponds to the timeseries of components from a PCA conducted on the combined
                                     WM and CSF masks voxel timeseries, including all components that together explain 50/100 of
                                     the variance, as in Muschelli et al. 2014.
                                     mean_FD: the mean framewise displacement timecourse
                                     """)
    confound_correction.add_argument('--FD_censoring', dest='FD_censoring', action='store_true', default=False,
                                     help="""
                                     Whether to remove timepoints that exceed a framewise displacement threshold.
                                     The frames that exceed the given threshold together with 1 back
                                     and 4 forward frames will be masked out (based on Power et al. 2012).
                                     """)
    confound_correction.add_argument('--FD_threshold', type=float, default=0.05,
                                     help="""
                                     Scrubbing threshold for the mean framewise displacement in mm (averaged across the brain
                                     mask) to select corrupted volumes.
                                     """)
    confound_correction.add_argument('--DVARS_censoring', dest='DVARS_censoring', action='store_true',default=False,
                                     help="""
                                     Whether to remove timepoints that present outlier values on the DVARS metric (temporal derivative
                                     of global signal).
                                     This will censor out timepoints until a distribution of DVARS values is obtained without outliers
                                     values above or below 2.5 standard deviations.
                                     """)
    confound_correction.add_argument('--minimum_timepoint', type=int,default=3,
                                     help="""
                                     Can select a threshold number of timepoint to remain after censoring, and return empty files for
                                     scans that don't pass threshold.
                                     """)
    confound_correction.add_argument('--standardize', dest='standardize', action='store_true',default=False,
                                     help="""
                                     Whether to standardize timeseries to unit variance.
                                     """)
    confound_correction.add_argument('--timeseries_interval', type=str, default='all',
                                     help="""
                                     Specify a time interval in the timeseries to keep. e.g. "0,80". By default all timeseries are kept.
                                     """)

    analysis.add_argument('confound_correction_out', action='store', type=Path,
                          help="""
                          path to RABIES confound regression output directory with the datasink.
                          """)
    analysis.add_argument('output_dir', action='store', type=Path,
                          help='path to drop analysis outputs.')
    analysis.add_argument('--output_name', type=str, default='analysis_wf',
                          help="""
                          Creates a new output folder to store the workflow of this analysis run, to avoid potential
                          overlaps with previous runs.
                          """)
    analysis.add_argument('--scan_list', type=str,
                          nargs="*",  # 0 or more values expected => creates a list
                          default=['all'],
                          help="""
                          This option offers to run the analysis on a subset of the scans.
                          The scans selected are specified by providing the full path to each EPI file from the input BIDS folder.
                          The list of scan can be specified manually as a list of file name '--scan_list scan1.nii.gz scan2.nii.gz ...'
                          or the files can be imbedded into a .txt file with one filename per row.
                          By default, 'all' will use all the scans previously processed.
                          """)
    analysis.add_argument('--seed_list', type=str,
                          nargs="*",  # 0 or more values expected => creates a list
                          default=[],
                          help="""
                          Can provide a list of seed .nii images that will be used to evaluate seed-based correlation maps
                          based on Pearson's r. Each seed must consist of a binary mask representing the ROI in commonspace.
                          """)
    analysis.add_argument('--seed_prior_list', type=str,
                          nargs="*",  # 0 or more values expected => creates a list
                          default=[],
                          help="""
                          For analysis QC of seed-based FC, prior network maps are required for each seed-FC provided in --seed_list.
                          Provide the list of prior files in matching order of the --seed_list inputs to match corresponding seed maps.
                          """)
    analysis.add_argument('--prior_maps', action='store', type=Path,
                            default=f"{rabies_path}/melodic_IC.nii.gz",
                            help="""
                            Provide a 4D nifti image with a series of spatial priors representing common sources of
                            signal (e.g. ICA components from a group-ICA run). This 4D prior map file will be used for 
                            Dual regression, Dual ICA and --data_diagnosis.
                            Default: Corresponds to a MELODIC run on a combined group of anesthetized-
                            ventilated and awake mice. Confound regression consisted of highpass at 0.01 Hz,
                            FD censoring at 0.03mm, DVARS censoring, and mot_6,WM_signal,CSF_signal as regressors.
                            """)
    analysis.add_argument('--prior_bold_idx', type=int,
                            nargs="*",  # 0 or more values expected => creates a list
                            default=[5, 12, 19],
                            help="""
                            Specify the indices for the priors to fit from --prior_maps. Only selected priors will be fitted 
                            for Dual ICA, and these priors will correspond to the BOLD components during --data_diagnosis.
                            """)
    analysis.add_argument('--prior_confound_idx', type=int,
                                nargs="*",  # 0 or more values expected => creates a list
                                default=[0, 1, 2, 6, 7, 8, 9, 10, 11,
                                         13, 14, 21, 22, 24, 26, 28, 29],
                                help="""
                                Specify the indices for the confound components from --prior_maps.
                                This is pertinent for the --data_diagnosis outputs.
                                """)
    analysis.add_argument("--data_diagnosis", dest='data_diagnosis', action='store_true',
                             help="""
                             This option carries out the spatiotemporal diagnosis as described in Desrosiers-Gregoire et al.
                             The diagnosis outputs key temporal and spatial features at the scan level allowing the
                             identification of sources of confounds in individual scans. A follow-up group-level correlation
                             between spatial features is also conducted to evaluate corruption of group-level outputs.
                             We recommend to conduct a data diagnosis from this workflow to complement FC analysis by
                             evaluating the intrinsic corruption of the dataset as well as the effectiveness of the confound
                             correction strategies.""")

    g_fc_matrix = analysis.add_argument_group(title='FC Matrix', description="""
        Options for performing a whole-brain timeseries correlation matrix analysis.
        """)
    g_fc_matrix.add_argument("--FC_matrix", dest='FC_matrix', action='store_true',
                             help="""
                             Choose this option to derive a whole-brain functional connectivity matrix, based on the
                             Pearson's r correlation of regional timeseries for each subject cleaned timeseries.
                             """)
    g_fc_matrix.add_argument("--ROI_type", type=str, default='parcellated',
                             choices=['parcellated', 'voxelwise'],
                             help="""
                             Define the types of ROI to extract regional timeseries for correlation matrix analysis.
                             Options are 'parcellated', in which case the atlas labels provided for preprocessing are used
                             as ROIs, or 'voxelwise', in which case all voxel timeseries are cross-correlated.
                             """)
    g_group_ICA = analysis.add_argument_group(title='Group ICA', description="""
        Options for performing group-ICA using FSL's MELODIC on the whole dataset cleaned timeseries.
        Note that confound regression must have been conducted on commonspace outputs.
        """)
    g_group_ICA.add_argument("--group_ICA", dest='group_ICA', action='store_true',
                             help="""
                             Choose this option to conduct group-ICA.
                             """)
    g_group_ICA.add_argument('--dim', type=int, default=0,
                             help="""
                             You can specify the number of ICA components to be derived. The default uses an automatic estimation.
                             """)
    g_group_ICA.add_argument('--melodic_random_seed', type=int, default=1,
                             help="""
                             For reproducibility, can manually set a random seed for MELODIC.
                             """)
    g_DR_ICA = analysis.add_argument_group(title='DR ICA', description="""
        Options for performing a dual regression analysis based on a previous group-ICA run from FSL's MELODIC.
        Note that confound regression must have been conducted on commonspace outputs.
        """)
    g_DR_ICA.add_argument("--DR_ICA", dest='DR_ICA', action='store_true',
                          help="""
                          Choose this option to conduct dual regression on each subject timeseries. This analysis will
                          output the spatial maps corresponding to the linear coefficients from the second linear
                          regression. See rabies.analysis_pkg.analysis_functions.dual_regression for the specific code.
                          """)
    g_dual_ICA = analysis.add_argument_group(title='Dual ICA', description="""
        Options for performing a Dual ICA.
        Need to provide the prior maps to fit --prior_maps, and the associated indices for the target components
        in --prior_bold_idx
        """)
    g_dual_ICA.add_argument('--dual_ICA', type=int, default=0,
                            help="""
                            Specify how many subject-specific sources to compute using dual ICA.
                            """)

    return parser


def execute_workflow():
    # generates the parser CLI and execute the workflow based on specified parameters.
    parser = get_parser()
    opts = parser.parse_args()

    try:
        output_folder = os.path.abspath(str(opts.output_dir))
    except:
        parser.print_help()
        return

    if not os.path.isdir(output_folder):
        os.makedirs(output_folder)

    # managing log info
    cli_file = f'{output_folder}/rabies_{opts.rabies_step}.pkl'
    if os.path.isfile(cli_file):
        raise ValueError(f"""
            A previous run was indicated by the presence of {cli_file}.
            This can lead to inconsistencies between previous outputs and the log files.
            To prevent this, you are required to manually remove {cli_file}, and we 
            recommend also removing previous datasinks from the {opts.rabies_step} RABIES step.
            """)

    with open(cli_file, 'wb') as handle:
        pickle.dump(opts, handle, protocol=pickle.HIGHEST_PROTOCOL)

    logging.basicConfig(filename=f'{output_folder}/rabies_{opts.rabies_step}.log', filemode='w',
                        format='%(asctime)s - %(levelname)s - %(message)s', level=os.environ.get("LOGLEVEL", "INFO"))
    log = logging.getLogger('root')

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    log.addHandler(handler)

    # verify default template installation
    install_DSURQE(log)

    from .__version__ import __version__
    log.info('Running RABIES - version: '+__version__)

    # print complete CLI command
    args = 'CLI INPUTS: \n'
    for arg in vars(opts):
        input = f'-> {arg} = {getattr(opts, arg)} \n'
        args += input
    log.info(args)

    if opts.rabies_step == 'preprocess':
        workflow = preprocess(opts, None, None, log)
    elif opts.rabies_step == 'confound_correction':
        workflow = confound_correction(opts, None, log)
    elif opts.rabies_step == 'analysis':
        workflow = analysis(opts, log)
    else:
        parser.print_help()
    workflow.base_dir = output_folder

    try:
        log.info(f'Running workflow with {opts.plugin} plugin.')
        # execute workflow, with plugin_args limiting the cluster load for parallel execution
        workflow.run(plugin=opts.plugin, plugin_args={'max_jobs': 50, 'dont_resubmit_completed_jobs': True,
                                                      'n_procs': opts.local_threads, 'qsub_args': f'-pe smp {str(opts.min_proc)}'})

    except Exception as e:
        log.critical(f'RABIES failed: {e}')
        raise


def preprocess(opts, cr_opts, analysis_opts, log):
    # Verify input and output directories
    data_dir_path = os.path.abspath(str(opts.bids_dir))
    output_folder = os.path.abspath(str(opts.output_dir))
    if not os.path.isdir(data_dir_path):
        raise ValueError("The provided BIDS data path doesn't exists.")
    else:
        # print the input data directory tree
        log.info("INPUT BIDS DATASET:  \n" + list_files(data_dir_path))

    if str(opts.data_type) == 'int16':
        opts.data_type = sitk.sitkInt16
    elif str(opts.data_type) == 'int32':
        opts.data_type = sitk.sitkInt32
    elif str(opts.data_type) == 'float32':
        opts.data_type = sitk.sitkFloat32
    elif str(opts.data_type) == 'float64':
        opts.data_type = sitk.sitkFloat64
    else:
        raise ValueError('Invalid --data_type provided.')


    # template options
    # if --bold_only, the default atlas files change to EPI versions
    if opts.bold_only:
        if str(opts.anat_template)==f"{rabies_path}/DSURQE_40micron_average.nii.gz":
            file=f"{rabies_path}/EPI_template.nii.gz"
            opts.anat_template=file
            log.info('With --bold_only, default --anat_template changed to '+file)
        if str(opts.brain_mask)==f"{rabies_path}/DSURQE_40micron_mask.nii.gz":
            file=f"{rabies_path}/EPI_brain_mask.nii.gz"
            opts.brain_mask=file
            log.info('With --bold_only, default --brain_mask changed to '+file)
        if str(opts.WM_mask)==f"{rabies_path}/DSURQE_40micron_eroded_WM_mask.nii.gz":
            file=f"{rabies_path}/EPI_WM_mask.nii.gz"
            opts.WM_mask=file
            log.info('With --bold_only, default --WM_mask changed to '+file)
        if str(opts.CSF_mask)==f"{rabies_path}/DSURQE_40micron_eroded_CSF_mask.nii.gz":
            file=f"{rabies_path}/EPI_CSF_mask.nii.gz"
            opts.CSF_mask=file
            log.info('With --bold_only, default --CSF_mask changed to '+file)
        if str(opts.vascular_mask)==f"{rabies_path}/vascular_mask.nii.gz":
            file=f"{rabies_path}/EPI_vascular_mask.nii.gz"
            opts.vascular_mask=file
            log.info('With --bold_only, default --vascular_mask changed to '+file)
        if str(opts.labels)==f"{rabies_path}/DSURQE_40micron_labels.nii.gz":
            file=f"{rabies_path}/EPI_labels.nii.gz"
            opts.labels=file
            log.info('With --bold_only, default --labels changed to '+file)
        if analysis_opts is not None:
            if str(analysis_opts.prior_maps)==f"{rabies_path}/melodic_IC.nii.gz":
                file=f"{rabies_path}/melodic_IC_resampled.nii.gz"
                analysis_opts.prior_maps=file
                log.info('With --bold_only, default --prior_maps changed to '+file)

    # make sure we have absolute paths
    opts.anat_template = os.path.abspath(opts.anat_template)
    opts.brain_mask = os.path.abspath(opts.brain_mask)
    opts.WM_mask = os.path.abspath(opts.WM_mask)
    opts.CSF_mask = os.path.abspath(opts.CSF_mask)
    opts.vascular_mask = os.path.abspath(opts.vascular_mask)
    opts.labels = os.path.abspath(opts.labels)

    # To use brain extraction, make sure either --commonspace_masking or --coreg_masking is used
    if opts.brain_extraction:
        if not (opts.commonspace_masking or opts.coreg_masking):
            raise ValueError(f"To use --brain_extraction, you must select either --commonspace_masking or --coreg_masking.")

    # convert template files to RAS convention if they aren't already
    from rabies.preprocess_pkg.utils import convert_to_RAS
    if not os.path.isfile(opts.anat_template):
        raise ValueError(f"--anat_template file {opts.anat_template} doesn't exists.")
    opts.anat_template = convert_to_RAS(
        str(opts.anat_template), output_folder+'/template_files')

    if not os.path.isfile(opts.brain_mask):
        raise ValueError(f"--brain_mask file {opts.brain_mask} doesn't exists.")
    check_binary_masks(opts.brain_mask)
    opts.brain_mask = convert_to_RAS(
        str(opts.brain_mask), output_folder+'/template_files')
    check_template_overlap(opts.anat_template, opts.brain_mask)

    if not os.path.isfile(opts.WM_mask):
        raise ValueError(f"--WM_mask file {opts.WM_mask} doesn't exists.")
    check_binary_masks(opts.WM_mask)
    opts.WM_mask = convert_to_RAS(
        str(opts.WM_mask), output_folder+'/template_files')
    check_template_overlap(opts.anat_template, opts.WM_mask)

    if not os.path.isfile(opts.CSF_mask):
        raise ValueError(f"--CSF_mask file {opts.CSF_mask} doesn't exists.")
    check_binary_masks(opts.CSF_mask)
    opts.CSF_mask = convert_to_RAS(
        str(opts.CSF_mask), output_folder+'/template_files')
    check_template_overlap(opts.anat_template, opts.CSF_mask)

    if not os.path.isfile(opts.vascular_mask):
        raise ValueError(f"--vascular_mask file {opts.vascular_mask} doesn't exists.")
    check_binary_masks(opts.vascular_mask)
    opts.vascular_mask = convert_to_RAS(
        str(opts.vascular_mask), output_folder+'/template_files')
    check_template_overlap(opts.anat_template, opts.vascular_mask)

    if not os.path.isfile(opts.labels):
        raise ValueError(f"--labels file {opts.labels} doesn't exists.")
    opts.labels = convert_to_RAS(
        str(opts.labels), output_folder+'/template_files')
    check_template_overlap(opts.anat_template, opts.labels)

    check_resampling_syntax(opts.nativespace_resampling)
    check_resampling_syntax(opts.commonspace_resampling)
    check_resampling_syntax(opts.anatomical_resampling)

    # write boilerplate
    boilerplate_file = f'{output_folder}/boilerplate.txt'

    methods,ref_string = preprocess_boilerplate(opts)
    txt_boilerplate="#######PREPROCESSING\n\n"+methods+ref_string+'\n\n'
    if cr_opts is not None:
        methods,ref_string = confound_correction_boilerplate(cr_opts)
        txt_boilerplate+="#######CONFOUND CORRECTION\n\n"+methods+ref_string+'\n\n'

    with open(boilerplate_file, "w") as text_file:
        text_file.write(txt_boilerplate)

    from rabies.main_wf import init_main_wf
    workflow = init_main_wf(data_dir_path, output_folder,
                            opts, cr_opts=cr_opts, analysis_opts=analysis_opts)

    # setting workflow options for debug mode
    if opts.debug:
        log.setLevel(os.environ.get("LOGLEVEL", "DEBUG"))

        # Change execution parameters
        workflow.config['execution'] = {'stop_on_first_crash': 'true',
                                        'remove_unnecessary_outputs': 'false',
                                        'keep_inputs': 'true'}

        # Change logging parameters
        workflow.config['logging'] = {'workflow_level': 'DEBUG',
                                      'filemanip_level': 'DEBUG',
                                      'interface_level': 'DEBUG',
                                      'utils_level': 'DEBUG'}
        log.debug('Debug ON')

    return workflow


def confound_correction(opts, analysis_opts, log):
    cli_file = f'{opts.preprocess_out}/rabies_preprocess.pkl'
    with open(cli_file, 'rb') as handle:
        preprocess_opts = pickle.load(handle)

    if opts.read_datasink:
        boilerplate_file = f'{opts.output_dir}/boilerplate_confound_correction.txt'
        methods,ref_string = confound_correction_boilerplate(opts)
        txt_boilerplate="#######CONFOUND CORRECTION\n\n"+methods+ref_string+'\n\n'
        with open(boilerplate_file, "w") as text_file:
            text_file.write(txt_boilerplate)

        from rabies.main_post import detached_confound_correction_wf
        workflow = detached_confound_correction_wf(preprocess_opts, opts, analysis_opts)
    else:
        workflow = preprocess(preprocess_opts, opts,
                            analysis_opts, log)

    return workflow


def analysis(opts, log):

    cli_file = f'{opts.confound_correction_out}/rabies_confound_correction.pkl'
    with open(cli_file, 'rb') as handle:
        confound_correction_opts = pickle.load(handle)

    workflow = confound_correction(confound_correction_opts, opts, log)

    return workflow

def install_DSURQE(log):

    install = False
    # verifies whether default template files are installed and installs them otherwise
    if not os.path.isfile(f"{rabies_path}/DSURQE_40micron_average.nii.gz"):
        install = True
    elif not os.path.isfile(f"{rabies_path}/DSURQE_40micron_mask.nii.gz"):
        install = True
    elif not os.path.isfile(f"{rabies_path}/DSURQE_40micron_eroded_WM_mask.nii.gz"):
        install = True
    elif not os.path.isfile(f"{rabies_path}/DSURQE_40micron_eroded_CSF_mask.nii.gz"):
        install = True
    elif not os.path.isfile(f"{rabies_path}/DSURQE_40micron_labels.nii.gz"):
        install = True
    elif not os.path.isfile(f"{rabies_path}/vascular_mask.nii.gz"):
        install = True
    elif not os.path.isfile(f"{rabies_path}/melodic_IC.nii.gz"):
        install = True
    elif not os.path.isfile(f"{rabies_path}/EPI_template.nii.gz"):
        install = True
    elif not os.path.isfile(f"{rabies_path}/EPI_brain_mask.nii.gz"):
        install = True
    elif not os.path.isfile(f"{rabies_path}/EPI_WM_mask.nii.gz"):
        install = True
    elif not os.path.isfile(f"{rabies_path}/EPI_CSF_mask.nii.gz"):
        install = True
    elif not os.path.isfile(f"{rabies_path}/EPI_vascular_mask.nii.gz"):
        install = True
    elif not os.path.isfile(f"{rabies_path}/EPI_labels.nii.gz"):
        install = True
    elif not os.path.isfile(f"{rabies_path}/melodic_IC_resampled.nii.gz"):
        install = True
    if install:
        from rabies.preprocess_pkg.utils import run_command
        log.info(
            "SOME FILES FROM THE DEFAULT TEMPLATE ARE MISSING. THEY WILL BE INSTALLED BEFORE FURTHER PROCESSING.")
        rc = run_command(f'install_DSURQE.sh {rabies_path}', verbose=True)


def check_binary_masks(mask):
    img = sitk.ReadImage(mask)
    array = sitk.GetArrayFromImage(img)
    if ((array != 1)*(array != 0)).sum() > 0:
        raise ValueError(
            f"The file {mask} is not a binary mask. Non-binary masks cannot be processed.")


def check_template_overlap(template, mask):
    template_img = sitk.ReadImage(template)
    mask_img = sitk.ReadImage(mask)
    if not template_img.GetOrigin() == mask_img.GetOrigin() and template_img.GetDirection() == mask_img.GetDirection():
        raise ValueError(
            f"The file {mask} does not appear to overlap with provided template {template}.")


def check_resampling_syntax(resampling):
    if resampling == 'inputs_defined':
        return
    try:
        if not 'x' in resampling:
            raise
        shape = resampling.split('x')
        if not len(shape) == 3:
            raise
        spacing = (float(shape[0]), float(shape[1]), float(shape[2]))
    except:
        raise ValueError(
            f"Resampling {resampling} must follow the format 'dim1xdim2xdim3', e.g. '0.1x0.1x0.1', (in mm) following the RAS axis convention.")


def list_files(startpath):
    string = ''
    for root, dirs, files in os.walk(startpath):
        level = root.replace(startpath, '').count(os.sep)
        indent = ' ' * 4 * (level)
        string += f'{indent}{os.path.basename(root)}/ \n'
        subindent = ' ' * 4 * (level + 1)
        for f in files:
            string += f'{subindent}{f} \n'
    return string

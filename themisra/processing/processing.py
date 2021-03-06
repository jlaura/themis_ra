import os
import logging
import sys

from themisra import MPI

import numpy as np
from scipy.optimize import differential_evolution

from plio.io import io_gdal
from plio.utils.utils import check_file_exists

import pvl
import gdal

import themisra.utils.utils as util
from themisra.wrappers import pipelinewrapper, isiswrapper

#constants
processingpipelines = {'themis_davinci':pipelinewrapper.themis_davinci}


sys_excepthook = sys.excepthook
def mpi_excepthook(v, t, tb):
    sys_excepthook(v, t, tb)
    MPI.COMM_WORLD.Abort(1)
sys.excepthook = mpi_excepthook

def process_header(job):
    """
    Given the input image and job instructions, check that the necessary
    header information is present to process.

    Parameters
    ----------
    job : dict
          Containing the PATH to an images

    Returns
    -------
    job : dict
          With updated, image header specific values

    """

    header = pvl.load(job['images'])
    bands = util.find_in_dict(header, 'BAND_BIN_BAND_NUMBER')
    #bands = header['BAND_BIN_BAND_NUMBER']

    #Extract the instrument name
    if not 'name' in job.keys() or job['name'] == None:
        instrument = util.find_in_dict(header, 'INSTRUMENT_NAME')
        job['name'] = util.instrumentmap[instrument]

    #Check that the required bands are present
    if not util.checkbandnumbers(bands, job['bands']):
        logger.error("Image {} contains bands {}.  Band(s) {} must be present.\n".format(i, bands, job['bands']))
        return

    if 'kerneluri' in job['projection'].keys():
        kernel = job['projection']['kerneluri']
    else:
        kernel = None

    return job

def preprocess_image(job, workingpath):
    """
    Preprocess a THEMIS EDR for Davinci using ISIS and write files into the
    workingpath.

    Parameters
    ----------
    job : dict
          A dictionary of job containing an image list and
          processing parameters

    workingpath : str
                  The working directory for intermediate files

    Returns
    -------

    outcube: str
               PATH to the preprocessed images.
    """
    # Check that the image exists
    image = job['images']

    if not check_file_exists(image):
        MPI.COMM_WORLD.Abort(1)

    logger = logging.getLogger(__name__)
    logger.info('Reading image {}'.format(image))

    # Process the image header
    job = process_header(job)
    if job is None:
        MPI.COMM_WORLD.Abort(1)

    basepath, fname = os.path.split(image)
    fname, _ = os.path.splitext(fname)

    #Convert to ISIS
    outcube = os.path.join(workingpath, '{}.cub'.format(fname))
    kernel = job.get('kernel', None)
    isiswrapper.preprocess_for_davinci(image, outcube, kernel)

    return outcube


def process_image(job, workingpath):
    """
    Process a THEMIS EDR using ISIS and Davinci to a level 2 map projected
    product. putting the output and intermediary files into the workingpath.

    Parameters
    ----------
    job : dict
          A dictionary of job containing an image list and
          processing parameters

    workingpath : str
                  The working directory for intermediate files

    Returns
    -------

    isiscube : str
               PATH to the processed ISIS cube

    startlocaltime : str
                     The image start time

    stoplocaltime : str
                    The image stop time
    """

    # path to the original image (no preprocessing)
    image = job['images']
    basepath, fname = os.path.split(image)
    fname, _ = os.path.splitext(fname)
    # path to the image that has been preprocessed for davinci
    dpp_image = os.path.join(workingpath, '{}.cub'.format(fname))

    if not check_file_exists(dpp_image):
        MPI.COMM_WORLD.Abort(1)

    logger = logging.getLogger(__name__)
    logger.info('Reading image {}'.format(dpp_image))
    # Process the image header

    job = process_header(job)
    if job is None:
        MPI.COMM_WORLD.Abort(1)

    #Convert to ISIS
    #Read from preprocessed image
    incidence, _, _ = isiswrapper.campt_header(dpp_image)

    # Process isomg Davinci
    deplaid = util.checkdeplaid(incidence)
    logger.info("If deplaid is set in the input parameters, using {} deplaid routines".format(deplaid))
    if 'deplaid' in job.keys():
        #User defined deplaid, cast to int for Davinci
        deplaid = int(job['deplaid'])
    else:
        #Fallback to standard deplaid
        deplaid = 1
        if deplaid == 'day':
            deplaid = 0

    dvcube = processingpipelines[job['processing_pipeline']](image, workingpath, deplaid,
                                                             job['uddw'], job['tesatm'],
                                                             job['rtilt'], job['force'])

    kernel = job.get('kernel', None)
    lats = job.get('lat_extent',[])
    lons = job.get('lon_extent', [])
    if lats:
        latlon_bounds = [lats[0], lats[1], lons[0], lons[1]]
    else:
        latlon_bounds = []
    isistemp = isiswrapper.postprocess_for_davinci(dvcube + '_temp.cub',
                                                   kernel=kernel,
                                                   latlon_bounds=latlon_bounds)

    isisrad =  isiswrapper.postprocess_for_davinci(dvcube + '_rad.cub',
                                                   kernel=kernel,
                                                   latlon_bounds=latlon_bounds)

    return isistemp, isisrad

def map_ancillary(isiscube, job):
    """
    Given the input image and job instructions, clip the associated ancillary
    data to the extents of the input image and write it to file.

    Parameters
    ----------
    isiscube : str
          The fully qualified path to the reference image
    job : dict
          Containing the PATH to an images

    Returns
    -------
    ancillary_data: dict
          With updated values of ancillary data

    """
    comm = MPI.COMM_WORLD
    rank = comm.rank
    basepath, fname = os.path.split(isiscube)
    fname, _ = os.path.splitext(fname)
    workingpath = basepath
    parameters = util.extract_metadata(isiscube, dict())

    if rank == 0:
        temperature = util.extract_temperature(isiscube)
        if temperature is None:
            logger.error('Failed to extract temperature data.')
            MPI.COMM_WORLD.Abort(1)

        shape =  list(temperature.raster_size)[::-1]
        reference_dataset = temperature
        reference_name = "temperature"

        # Extract the ancillary data
        ancillary_data = util.extract_ancillary_data(job, temperature, parameters, workingpath, shape, reference_dataset)
        return ancillary_data


def optimize_pixel(obs3, obs9, rock3, rock9):
    """Find the optimal value for a single pixel using differential evolution
         technique.

    Parameters
    ----------

    obs3:       float
                The value for this pixel that was observed in band 3 of the
                  input image.

    obs9:       float
                The value for this pixel that was observed in band 9 of the
                  input image.

    rock3:      float
                The expected value for rock in band 3.

    rock9:      float
                The expected value for rock in band 9.

    Returns
    -------
    res:        list
                alpha, fine3, fine9

    """
    bounds = [(0,1), #alpha
              (160,250), #Fine3
              (160,250)] #Fine9
    res = differential_evolution(util.cost_func, bounds,
                                 args=(obs3, obs9, rock3,rock9),
                                 strategy='best1bin')
    alpha, fine3, fine9 = res['x']

    return [alpha, fine3, fine9, res['success'], res['nit']]

def optimize_all(obs3, obs9, rock3, rock9):
    # Arrays to hold alpha, fine3, fine9, b_success, n_iter
    out = [ [], [], [], [], [] ]

    #@@TODO assumes that obs3, obs9, rock3, rock9 are the same size.
    for i in range(len(obs3)):
         params = optimize_pixel(obs3, obs9, rock3, rock9)
         #append pixel values to corresponding lists
         for j in range(len(params)):
             out[j].append(params[j])

    return out



def write_to_gtiff(bands, isiscube, job):
    driver = gdal.GetDriverByName('GTiff')
    bands = len(bands)
    bittype = 'GDT_Float64'

    temperature = util.extract_temperature(isiscube)
    td_g = temperature.read_array()
    y, x = td_g.shape

    outpath = os.path.join(job['outpath'], temperature.base_name)

    dataset = driver.Create('{}.tif'.format(outpath), x, y, bands, getattr(gdal, bittype))

    ndv = None

    try:
        ndv = temperature.no_data_value
    except:
        # intentionally left blank
        pass

    for i in range(len(bands)):
        bnd = dataset.GetRasterBand(i)
        if ndv != None:
            bnd.SetNoDataValue(ndv)
        bnd.WriteArray(bands(band))
        dataset.FlushCache()

    return outpath

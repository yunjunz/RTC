#!/usr/bin/env python

'''
RTC Workflow
'''

import os
import time

import logging
import numpy as np
from osgeo import gdal
import argparse

import isce3

from s1reader.s1_burst_slc import Sentinel1BurstSlc

from rtc.geogrid import snap_coord
from rtc.runconfig import RunConfig
from rtc.mosaic_geobursts import weighted_mosaic
from rtc.core import create_logger
from rtc.h5_prep import save_hdf5_file, create_hdf5_file, \
    save_hdf5_dataset, BASE_DS

logger = logging.getLogger('rtc_s1')


def _update_mosaic_boundaries(mosaic_geogrid_dict, geogrid):
    """Updates mosaic boundaries and check if pixel spacing
       and EPSG code are consistent between burst
       and mosaic geogrid

       Parameters
       ----------
       mosaic_geogrid_dict: dict
              Dictionary containing mosaic geogrid parameters
       geogrid : isce3.product.GeoGridParameters
              Burst geogrid

    """
    xf = geogrid.start_x + geogrid.spacing_x * geogrid.width
    yf = geogrid.start_y + geogrid.spacing_y * geogrid.length
    if ('x0' not in mosaic_geogrid_dict.keys() or
            geogrid.start_x < mosaic_geogrid_dict['x0']):
        mosaic_geogrid_dict['x0'] = geogrid.start_x
    if ('xf' not in mosaic_geogrid_dict.keys() or
            xf > mosaic_geogrid_dict['xf']):
        mosaic_geogrid_dict['xf'] = xf
    if ('y0' not in mosaic_geogrid_dict.keys() or
            geogrid.start_y > mosaic_geogrid_dict['y0']):
        mosaic_geogrid_dict['y0'] = geogrid.start_y
    if ('yf' not in mosaic_geogrid_dict.keys() or
            yf < mosaic_geogrid_dict['yf']):
        mosaic_geogrid_dict['yf'] = yf
    if 'dx' not in mosaic_geogrid_dict.keys():
        mosaic_geogrid_dict['dx'] = geogrid.spacing_x
    else:
        assert(mosaic_geogrid_dict['dx'] == geogrid.spacing_x)
    if 'dy' not in mosaic_geogrid_dict.keys():
        mosaic_geogrid_dict['dy'] = geogrid.spacing_y
    else:
        assert(mosaic_geogrid_dict['dy'] == geogrid.spacing_y)
    if 'epsg' not in mosaic_geogrid_dict.keys():
        mosaic_geogrid_dict['epsg'] = geogrid.epsg
    else:
        assert(mosaic_geogrid_dict['epsg'] == geogrid.epsg)


def _create_raster_obj(output_dir, ds_name, ds_hdf5, dtype, shape,
                radar_grid_file_dict, output_obj_list, flag_save_vector_1,
                extension):
    if flag_save_vector_1 is not True:
        return None

    output_file = os.path.join(output_dir, ds_name) + '.' + extension
    raster_obj = isce3.io.Raster(
        output_file,
        shape[2],
        shape[1],
        shape[0],
        dtype,
        "GTiff")
    output_obj_list.append(raster_obj)
    radar_grid_file_dict[ds_hdf5] = output_file
    return raster_obj


def _add_output_to_output_metadata_dict(flag, key, output_dir,
        output_metadata_dict, product_id, extension):
    if not flag:
        return
    output_image_list = []
    output_metadata_dict[key] = \
        [os.path.join(output_dir, f'{product_id}_{key}.{extension}'),
                      output_image_list]


def apply_slc_corrections(burst_in: Sentinel1BurstSlc,
                          path_slc_vrt: str,
                          path_slc_out: str,
                          flag_output_complex: bool = False,
                          flag_thermal_correction: bool = True,
                          flag_apply_abs_rad_correction: bool = True):
    '''Apply thermal correction stored in burst_in. Save the corrected signal
    back to ENVI format. Preserves the phase.'''

    # Load the SLC of the burst
    burst_in.slc_to_vrt_file(path_slc_vrt)
    slc_gdal_ds = gdal.Open(path_slc_vrt)
    arr_slc_from = slc_gdal_ds.ReadAsArray()

    # Apply the correction
    if flag_thermal_correction:
        logger.info(f'applying thermal noise correction to burst SLCs')
        corrected_image = np.abs(arr_slc_from) ** 2 - burst_in.thermal_noise_lut
        min_backscatter = 0
        max_backscatter = None
        corrected_image = np.clip(corrected_image, min_backscatter,
                                  max_backscatter)
    else:
        corrected_image=np.abs(arr_slc_from) ** 2

    if flag_apply_abs_rad_correction:
        logger.info(f'applying absolute radiometric correction to burst SLCs')
    if flag_output_complex:
        factor_mag = np.sqrt(corrected_image) / np.abs(arr_slc_from)
        factor_mag[np.isnan(factor_mag)] = 0.0
        corrected_image = arr_slc_from * factor_mag
        dtype = gdal.GDT_CFloat32
        if flag_apply_abs_rad_correction:
            corrected_image = \
                corrected_image / burst_in.burst_calibration.beta_naught
    else:
        dtype = gdal.GDT_Float32
        if flag_apply_abs_rad_correction:
            corrected_image = \
                corrected_image / burst_in.burst_calibration.beta_naught ** 2

    # Save the corrected image
    drvout = gdal.GetDriverByName('GTiff')
    raster_out = drvout.Create(path_slc_out, burst_in.shape[1],
                               burst_in.shape[0], 1, dtype)
    band_out = raster_out.GetRasterBand(1)
    band_out.WriteArray(corrected_image)
    band_out.FlushCache()
    del band_out


def run(cfg):
    '''
    Run geocode burst workflow with user-defined
    args stored in dictionary runconfig `cfg`

    Parameters
    ---------
    cfg: dict
        Dictionary with user runconfig options
    '''

    # Start tracking processing time
    t_start = time.time()
    time_stamp = str(float(time.time()))
    logger.info("Starting geocode burst")

    # unpack processing parameters
    processing_namespace = cfg.groups.processing
    dem_interp_method_enum = \
        processing_namespace.dem_interpolation_method_enum
    flag_apply_rtc = processing_namespace.apply_rtc
    flag_apply_thermal_noise_correction = \
        processing_namespace.apply_thermal_noise_correction
    flag_apply_abs_rad_correction = \
        processing_namespace.apply_absolute_radiometric_correction

    # read product path group / output format
    product_id = cfg.groups.product_path_group.product_id
    if product_id is None:
        product_id = 'rtc_product'

    scratch_path = os.path.join(
        cfg.groups.product_path_group.scratch_path, f'temp_{time_stamp}')
    output_dir = cfg.groups.product_path_group.output_dir
    flag_mosaic = cfg.groups.product_path_group.mosaic_bursts

    output_format = cfg.groups.product_path_group.output_format

    flag_hdf5 = (output_format == 'HDF5' or output_format == 'NETCDF')

    if output_format == 'NETCDF':
        hdf5_file_extension = 'nc'
    else:
        hdf5_file_extension = 'h5'

    if flag_hdf5:
        output_raster_format = 'GTiff'
    else:
        output_raster_format = output_format

    if output_raster_format == 'GTiff':
        extension = 'tif'
    else:
        extension = 'bin'

    # unpack geocode run parameters
    geocode_namespace = cfg.groups.processing.geocoding

    if cfg.groups.processing.geocoding.algorithm_type == "area_projection":
        geocode_algorithm = isce3.geocode.GeocodeOutputMode.AREA_PROJECTION
    else:
        geocode_algorithm = isce3.geocode.GeocodeOutputMode.INTERP

    memory_mode = geocode_namespace.memory_mode
    geogrid_upsampling = geocode_namespace.geogrid_upsampling
    abs_cal_factor = geocode_namespace.abs_rad_cal
    clip_max = geocode_namespace.clip_max
    clip_min = geocode_namespace.clip_min
    # geogrids = geocode_namespace.geogrids
    flag_upsample_radar_grid = geocode_namespace.upsample_radargrid
    flag_save_incidence_angle = geocode_namespace.save_incidence_angle
    flag_save_local_inc_angle = geocode_namespace.save_local_inc_angle
    flag_save_projection_angle = geocode_namespace.save_projection_angle
    flag_save_rtc_anf_psi = geocode_namespace.save_rtc_anf_psi
    flag_save_range_slope = \
        geocode_namespace.save_range_slope
    flag_save_nlooks = geocode_namespace.save_nlooks
    flag_save_rtc_anf = geocode_namespace.save_rtc_anf
    flag_save_dem = geocode_namespace.save_dem

    flag_call_radar_grid = (flag_save_incidence_angle or
        flag_save_local_inc_angle or flag_save_projection_angle or
        flag_save_rtc_anf_psi or flag_save_dem or
        flag_save_range_slope)

    # unpack RTC run parameters
    rtc_namespace = cfg.groups.processing.rtc

    # only 2 RTC algorithms supported: area_projection (default) &
    # bilinear_distribution
    if rtc_namespace.algorithm_type == "bilinear_distribution":
        rtc_algorithm = isce3.geometry.RtcAlgorithm.RTC_BILINEAR_DISTRIBUTION
    else:
        rtc_algorithm = isce3.geometry.RtcAlgorithm.RTC_AREA_PROJECTION

    output_terrain_radiometry = rtc_namespace.output_type
    input_terrain_radiometry = rtc_namespace.input_terrain_radiometry
    rtc_min_value_db = rtc_namespace.rtc_min_value_db
    rtc_upsampling = rtc_namespace.dem_upsampling
    if (flag_apply_rtc and output_terrain_radiometry ==
            isce3.geometry.RtcOutputTerrainRadiometry.SIGMA_NAUGHT):
        output_radiometry_str = "radar backscatter sigma0"
    elif (flag_apply_rtc and output_terrain_radiometry ==
            isce3.geometry.RtcOutputTerrainRadiometry.GAMMA_NAUGHT):
        output_radiometry_str = 'radar backscatter gamma0'
    elif input_terrain_radiometry == isce3.geometry.RtcInputTerrainRadiometry.BETA_NAUGHT:
        output_radiometry_str = 'radar backscatter beta0'
    else:
        output_radiometry_str = 'radar backscatter sigma0'

    # Common initializations
    dem_raster = isce3.io.Raster(cfg.dem)
    epsg = dem_raster.get_epsg()
    proj = isce3.core.make_projection(epsg)
    ellipsoid = proj.ellipsoid
    zero_doppler = isce3.core.LUT2d()
    threshold = cfg.geo2rdr_params.threshold
    maxiter = cfg.geo2rdr_params.numiter
    exponent = 1 if (flag_apply_thermal_noise_correction or
                     flag_apply_abs_rad_correction) else 2

    # output mosaics
    geo_filename = f'{output_dir}/'f'{product_id}.{extension}'
    output_imagery_list = []
    output_file_list = []
    output_metadata_dict = {}

    if flag_hdf5:
        output_dir_mosaic_raster = scratch_path
    else:
        output_dir_mosaic_raster = output_dir

    _add_output_to_output_metadata_dict(
        flag_save_nlooks, 'nlooks', output_dir_mosaic_raster,
        output_metadata_dict, product_id, extension)
    _add_output_to_output_metadata_dict(
        flag_save_rtc_anf, 'rtc', output_dir_mosaic_raster,
        output_metadata_dict, product_id, extension)

    mosaic_geogrid_dict = {}
    temp_files_list = []

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(scratch_path, exist_ok=True)
    vrt_options_mosaic = gdal.BuildVRTOptions(separate=True)

    n_bursts = len(cfg.bursts.items())
    print('number of bursts to process:', n_bursts)

    hdf5_obj = None
    output_hdf5_file = os.path.join(output_dir,
                                    f'{product_id}.{hdf5_file_extension}')
    # iterate over sub-burts
    for burst_index, (burst_id, burst_pol_dict) in enumerate(cfg.bursts.items()):
        
        # ===========================================================
        # start burst processing

        t_burst_start = time.time()
        logger.info(f'processing burst: {burst_id} ({burst_index+1}/'
                    f'{n_bursts})')

        pols = list(burst_pol_dict.keys())
        burst = burst_pol_dict[pols[0]]

        flag_bursts_files_are_temporary = \
            flag_hdf5 or (flag_mosaic and not n_bursts == 1)

        burst_scratch_path = f'{scratch_path}/{burst_id}/'
        os.makedirs(burst_scratch_path, exist_ok=True)

        if flag_bursts_files_are_temporary:
            bursts_output_dir = burst_scratch_path
        else:
            bursts_output_dir = os.path.join(output_dir, burst_id)
            os.makedirs(bursts_output_dir, exist_ok=True)
        
        geogrid = cfg.geogrids[burst_id]

        # snap coordinates
        x_snap = geogrid.spacing_x
        y_snap = geogrid.spacing_y
        geogrid.start_x = snap_coord(geogrid.start_x, x_snap, np.floor)
        geogrid.start_y = snap_coord(geogrid.start_y, y_snap, np.ceil)

        # update mosaic boundaries
        _update_mosaic_boundaries(mosaic_geogrid_dict, geogrid)

        logger.info(f'reading burst SLCs')
        radar_grid = burst.as_isce3_radargrid()
        # native_doppler = burst.doppler.lut2d
        orbit = burst.orbit
        if 'orbit' not in mosaic_geogrid_dict.keys():
            mosaic_geogrid_dict['orbit'] = orbit
        if 'wavelength' not in mosaic_geogrid_dict.keys():
            mosaic_geogrid_dict['wavelength'] = burst.wavelength
        if 'lookside' not in mosaic_geogrid_dict.keys():
            mosaic_geogrid_dict['lookside'] = radar_grid.lookside

        input_file_list = []
        pol_list = list(burst_pol_dict.keys())
        for pol, burst_pol in burst_pol_dict.items():
            temp_slc_path = \
                f'{burst_scratch_path}/rslc_{pol}.vrt'
            temp_slc_corrected_path = (
                f'{burst_scratch_path}/rslc_{pol}_corrected.{extension}')
            burst_pol.slc_to_vrt_file(temp_slc_path)

            if (flag_apply_thermal_noise_correction or
                    flag_apply_abs_rad_correction):
                apply_slc_corrections(
                    burst_pol,
                    temp_slc_path,
                    temp_slc_corrected_path,
                    flag_output_complex=False,
                    flag_thermal_correction =
                        flag_apply_thermal_noise_correction,
                    flag_apply_abs_rad_correction=True)
                input_burst_filename = temp_slc_corrected_path
            else:
                input_burst_filename = temp_slc_path

            temp_files_list.append(input_burst_filename)
            input_file_list.append(input_burst_filename)

        # create multi-band VRT
        if len(input_file_list) == 1:
            rdr_burst_raster = isce3.io.Raster(input_file_list[0])
        else:
            temp_vrt_path = f'{burst_scratch_path}/rslc.vrt'
            gdal.BuildVRT(temp_vrt_path, input_file_list,
                          options=vrt_options_mosaic)
            rdr_burst_raster = isce3.io.Raster(temp_vrt_path)
            temp_files_list.append(temp_vrt_path)

        # Generate output geocoded burst raster
        if flag_bursts_files_are_temporary:
            # files are temporary
            geo_burst_filename = \
                f'{burst_scratch_path}/{product_id}.{extension}'
            temp_files_list.append(geo_burst_filename)
        else:
            os.makedirs(f'{output_dir}/{burst_id}', exist_ok=True)
            geo_burst_filename = \
                f'{output_dir}/{burst_id}/{product_id}.{extension}'
            output_file_list.append(geo_burst_filename)
        
        geo_burst_raster = isce3.io.Raster(
            geo_burst_filename,
            geogrid.width, geogrid.length,
            rdr_burst_raster.num_bands, gdal.GDT_Float32,
            output_raster_format)

        # init Geocode object depending on raster type
        if rdr_burst_raster.datatype() == gdal.GDT_Float32:
            geo_obj = isce3.geocode.GeocodeFloat32()
        elif rdr_burst_raster.datatype() == gdal.GDT_Float64:
            geo_obj = isce3.geocode.GeocodeFloat64()
        elif rdr_burst_raster.datatype() == gdal.GDT_CFloat32:
            geo_obj = isce3.geocode.GeocodeCFloat32()
        elif rdr_burst_raster.datatype() == gdal.GDT_CFloat64:
            geo_obj = isce3.geocode.GeocodeCFloat64()
        else:
            err_str = 'Unsupported raster type for geocoding'
            raise NotImplementedError(err_str)

        # init geocode members
        geo_obj.orbit = orbit
        geo_obj.ellipsoid = ellipsoid
        geo_obj.doppler = zero_doppler
        geo_obj.threshold_geo2rdr = threshold
        geo_obj.numiter_geo2rdr = maxiter

        # set data interpolator based on the geocode algorithm
        if geocode_algorithm == isce3.geocode.GeocodeOutputMode.INTERP:
            geo_obj.data_interpolator = geocode_algorithm

        geo_obj.geogrid(geogrid.start_x, geogrid.start_y,
                        geogrid.spacing_x, geogrid.spacing_y,
                        geogrid.width, geogrid.length, geogrid.epsg)

        if flag_save_nlooks:
            nlooks_file = (f'{bursts_output_dir}/{product_id}'
                           f'_nlooks.{extension}')
            if flag_bursts_files_are_temporary:
                temp_files_list.append(nlooks_file)
            else:
                output_file_list.append(nlooks_file)
            out_geo_nlooks_obj = isce3.io.Raster(
                nlooks_file, geogrid.width, geogrid.length, 1,
                gdal.GDT_Float32, output_raster_format)
        else:
            nlooks_file = None
            out_geo_nlooks_obj = None

        if flag_save_rtc_anf:
            rtc_anf_file = (f'{bursts_output_dir}/{product_id}'
               f'_rtc_anf.{extension}')
            if flag_bursts_files_are_temporary:
                temp_files_list.append(rtc_anf_file)
            else:
                output_file_list.append(rtc_anf_file)
            out_geo_rtc_obj = isce3.io.Raster(
                rtc_anf_file,
                geogrid.width, geogrid.length, 1,
                gdal.GDT_Float32, output_raster_format)
        else:
            rtc_anf_file = None
            out_geo_rtc_obj = None

        # Extract burst boundaries and create sub_swaths object to mask
        # invalid radar samples
        n_subswaths = 1
        sub_swaths = isce3.product.SubSwaths(radar_grid.length,
                                             radar_grid.width,
                                             n_subswaths)
        last_range_sample = min([burst.last_valid_sample, radar_grid.width])
        valid_samples_sub_swath = np.repeat(
            [[burst.first_valid_sample, last_range_sample + 1]],
            radar_grid.length, axis=0)
        for i in range(burst.first_valid_line):
            valid_samples_sub_swath[i, :] = 0
        for i in range(burst.last_valid_line, radar_grid.length):
            valid_samples_sub_swath[i, :] = 0
        
        sub_swaths.set_valid_samples_array(1, valid_samples_sub_swath)

        # geocode
        geo_obj.geocode(radar_grid=radar_grid,
                        input_raster=rdr_burst_raster,
                        output_raster=geo_burst_raster,
                        dem_raster=dem_raster,
                        output_mode=geocode_algorithm,
                        geogrid_upsampling=geogrid_upsampling,
                        flag_apply_rtc=flag_apply_rtc,
                        input_terrain_radiometry=input_terrain_radiometry,
                        output_terrain_radiometry=output_terrain_radiometry,
                        exponent=exponent,
                        rtc_min_value_db=rtc_min_value_db,
                        rtc_upsampling=rtc_upsampling,
                        rtc_algorithm=rtc_algorithm,
                        abs_cal_factor=abs_cal_factor,
                        flag_upsample_radar_grid=flag_upsample_radar_grid,
                        clip_min = clip_min,
                        clip_max = clip_max,
                        # radargrid_nlooks=radar_grid_nlooks,
                        # out_off_diag_terms=out_off_diag_terms_obj,
                        out_geo_nlooks=out_geo_nlooks_obj,
                        out_geo_rtc=out_geo_rtc_obj,
                        # out_geo_dem=out_geo_dem_obj,
                        input_rtc=None,
                        output_rtc=None,
                        dem_interp_method=dem_interp_method_enum,
                        memory_mode=memory_mode,
                        sub_swaths=sub_swaths)

        del geo_burst_raster
        if not flag_bursts_files_are_temporary:
            logger.info(f'file saved: {geo_burst_filename}')
        output_imagery_list.append(geo_burst_filename)

        if flag_save_nlooks:
            del out_geo_nlooks_obj
            if not flag_bursts_files_are_temporary:
                logger.info(f'file saved: {nlooks_file}')
            output_metadata_dict['nlooks'][1].append(nlooks_file)
    
        if flag_save_rtc_anf:
            del out_geo_rtc_obj
            if not flag_bursts_files_are_temporary:
                logger.info(f'file saved: {rtc_anf_file}')
            output_metadata_dict['rtc'][1].append(rtc_anf_file)

        radar_grid_file_dict = {}
        if flag_call_radar_grid and not flag_mosaic:
            get_radar_grid(
                geogrid, dem_interp_method_enum, product_id,
                bursts_output_dir, extension, flag_save_incidence_angle,
                flag_save_local_inc_angle, flag_save_projection_angle,
                flag_save_rtc_anf_psi,
                flag_save_range_slope, flag_save_dem,
                dem_raster, radar_grid_file_dict,
                mosaic_geogrid_dict, orbit,
                verbose = not flag_bursts_files_are_temporary)
            if flag_hdf5:
                # files are temporary
                temp_files_list += list(radar_grid_file_dict.values())
            else:
                output_file_list += list(radar_grid_file_dict.values())

        if flag_hdf5 and not flag_mosaic:
            hdf5_file_output_dir = os.path.join(output_dir, burst_id)
            os.makedirs(hdf5_file_output_dir, exist_ok=True)
            output_hdf5_file =  os.path.join(
                hdf5_file_output_dir, f'{product_id}.{hdf5_file_extension}')
            hdf5_obj = create_hdf5_file(output_hdf5_file, orbit, burst, cfg)
            save_hdf5_file(
                hdf5_obj, output_hdf5_file, flag_apply_rtc,
                clip_max, clip_min, output_radiometry_str, output_file_list,
                geogrid, pol_list, geo_burst_filename, nlooks_file,
                rtc_anf_file, radar_grid_file_dict)
        elif flag_hdf5 and flag_mosaic and burst_index == 0:
            hdf5_obj = create_hdf5_file(output_hdf5_file, orbit, burst, cfg)

        t_burst_end = time.time()
        logger.info(
            f'elapsed time (burst): {t_burst_end - t_burst_start}')

        # end burst processing
        # ===========================================================

    if flag_call_radar_grid and flag_mosaic:
        radar_grid_file_dict = {}

        if flag_hdf5:
            radar_grid_output_dir = scratch_path
        else:
            radar_grid_output_dir = output_dir
        get_radar_grid(cfg.geogrid, dem_interp_method_enum, product_id,
                       radar_grid_output_dir, extension, flag_save_incidence_angle,
                       flag_save_local_inc_angle, flag_save_projection_angle,
                       flag_save_rtc_anf_psi,
                       flag_save_range_slope, flag_save_dem,
                       dem_raster, radar_grid_file_dict,
                       mosaic_geogrid_dict,
                       orbit, verbose = not flag_hdf5)
        if flag_hdf5:
            # files are temporary
            temp_files_list += list(radar_grid_file_dict.values())
        else:
            output_file_list += list(radar_grid_file_dict.values())

    if flag_mosaic:
        # mosaic sub-bursts
        geo_filename = f'{output_dir_mosaic_raster}/{product_id}.{extension}'
        logger.info(f'mosaicking file: {geo_filename}')

        nlooks_list = output_metadata_dict['nlooks'][1]
        weighted_mosaic(output_imagery_list, nlooks_list,
                        geo_filename, cfg.geogrid, verbose=False)

        if flag_hdf5:
            temp_files_list.append(geo_filename)
        else:
            output_file_list.append(geo_filename)

        # mosaic other bands
        for key in output_metadata_dict.keys():
            output_file, input_files = output_metadata_dict[key]
            logger.info(f'mosaicking file: {output_file}')
            weighted_mosaic(input_files, nlooks_list, output_file,
                            cfg.geogrid, verbose=False)
            if flag_hdf5:
                temp_files_list.append(output_file)
            else:
                output_file_list.append(output_file)

        if flag_hdf5:
            if flag_save_nlooks:
                nlooks_mosaic_file = output_metadata_dict['nlooks'][0]
            else:
                nlooks_mosaic_file = None
            if flag_save_rtc_anf:
                rtc_anf_mosaic_file = output_metadata_dict['rtc'][0]
            else:
                rtc_anf_mosaic_file = None

            # Update metadata datasets that depend on all bursts
            sensing_start = None
            sensing_stop = None
            for burst_id, burst_pol_dict in cfg.bursts.items():
                pols = list(burst_pol_dict.keys())
                burst = burst_pol_dict[pols[0]]
                print('this burst:')
                if sensing_start is not None:
                    print('    ', sensing_start.strftime('%Y-%m-%dT%H:%M:%S.%f'))
                if sensing_stop is not None:
                    print('    ', sensing_stop.strftime('%Y-%m-%dT%H:%M:%S.%f'))
                if (sensing_start is None or
                        burst.sensing_start < sensing_start):
                    sensing_start = burst.sensing_start
                    print('updated sensing start')
                if sensing_stop is None or burst.sensing_stop > sensing_stop:
                    sensing_stop = burst.sensing_stop
                    print('updated sensing stop')

            sensing_start_ds = f'{BASE_DS}/identification/zeroDopplerStartTime'
            sensing_end_ds = f'{BASE_DS}/identification/zeroDopplerEndTime'
            del hdf5_obj[sensing_start_ds]
            del hdf5_obj[sensing_end_ds]
            hdf5_obj[sensing_start_ds] = \
                sensing_start.strftime('%Y-%m-%dT%H:%M:%S.%f')
            hdf5_obj[sensing_end_ds] = \
                sensing_stop.strftime('%Y-%m-%dT%H:%M:%S.%f')

            save_hdf5_file(hdf5_obj, output_hdf5_file, flag_apply_rtc,
                           clip_max, clip_min, output_radiometry_str,
                           output_file_list, cfg.geogrid, pol_list,
                           geo_filename, nlooks_mosaic_file,
                           rtc_anf_mosaic_file, radar_grid_file_dict)

    logger.info('removing temporary files:')
    for filename in temp_files_list:
        if not os.path.isfile(filename):
            continue
        os.remove(filename)
        logger.info(f'    {filename}')

    logger.info('output files:')
    for filename in output_file_list:
        logger.info(f'    {filename}')

    t_end = time.time()
    logger.info(f'elapsed time: {t_end - t_start}')



def get_radar_grid(geogrid, dem_interp_method_enum, product_id,
                   output_dir, extension, flag_save_incidence_angle,
                   flag_save_local_inc_angle, flag_save_projection_angle,
                   flag_save_rtc_anf_psi,
                   flag_save_range_slope, flag_save_dem, dem_raster,
                   radar_grid_file_dict, mosaic_geogrid_dict, orbit,
                   verbose = True):
    output_obj_list = []
    layers_nbands = 1
    shape = [layers_nbands, geogrid.length, geogrid.width]

    incidence_angle_raster = _create_raster_obj(
            output_dir, f'{product_id}_incidence_angle',
            'incidenceAngle', gdal.GDT_Float32, shape, radar_grid_file_dict,
            output_obj_list, flag_save_incidence_angle, extension)
    local_incidence_angle_raster = _create_raster_obj(
            output_dir, f'{product_id}_local_incidence_angle',
            'localIncidenceAngle', gdal.GDT_Float32, shape,
            radar_grid_file_dict, output_obj_list, flag_save_local_inc_angle,
            extension)
    projection_angle_raster = _create_raster_obj(
            output_dir, f'{product_id}_projection_angle',
            'projectionAngle', gdal.GDT_Float32, shape, radar_grid_file_dict,
            output_obj_list, flag_save_projection_angle, extension)
    rtc_anf_psi_raster = _create_raster_obj(
            output_dir, f'{product_id}_rtc_anf_psi',
            'areaNormalizationFactorPsi', gdal.GDT_Float32, shape,
            radar_grid_file_dict, output_obj_list, 
            flag_save_rtc_anf_psi, extension)
    range_slope_raster = _create_raster_obj(
            output_dir, f'{product_id}_range_slope',
            'rangeSlope', gdal.GDT_Float32, shape, radar_grid_file_dict,
            output_obj_list, flag_save_range_slope, extension)
    interpolated_dem_raster = _create_raster_obj(
            output_dir, f'{product_id}_interpolated_dem',
            'interpolatedDem', gdal.GDT_Float32, shape, radar_grid_file_dict,
            output_obj_list, flag_save_dem, extension)

    # TODO review this (Doppler)!!!
    # native_doppler = burst.doppler.lut2d
    native_doppler = isce3.core.LUT2d()
    native_doppler.bounds_error = False
    grid_doppler = isce3.core.LUT2d()
    grid_doppler.bounds_error = False

    # call get_radar_grid()
    isce3.geogrid.get_radar_grid(mosaic_geogrid_dict['lookside'],
                                 mosaic_geogrid_dict['wavelength'],
                                 dem_raster,
                                 geogrid,
                                 orbit,
                                 native_doppler,
                                 grid_doppler,
                                 incidence_angle_raster =
                                    incidence_angle_raster,
                                 local_incidence_angle_raster =
                                    local_incidence_angle_raster,
                                 projection_angle_raster =
                                    projection_angle_raster,
                                 simulated_radar_brightness_raster =
                                    rtc_anf_psi_raster,
                                 directional_slope_angle_raster =
                                    range_slope_raster,
                                 interpolated_dem_raster =
                                    interpolated_dem_raster,
                                 dem_interp_method=dem_interp_method_enum)

    # Flush data
    for obj in output_obj_list:
        del obj

    if not verbose:
        return


def _load_parameters(cfg):
    '''
    Load GCOV specific parameters.
    '''

    geocode_namespace = cfg.groups.processing.geocoding
    rtc_namespace = cfg.groups.processing.rtc

    if geocode_namespace.clip_max is None:
        geocode_namespace.clip_max = np.nan

    if geocode_namespace.clip_min is None:
        geocode_namespace.clip_min = np.nan

    if geocode_namespace.geogrid_upsampling is None:
        geocode_namespace.geogrid_upsampling = 1.0

    if geocode_namespace.memory_mode == 'single_block':
        geocode_namespace.memory_mode = \
            isce3.core.GeocodeMemoryMode.SingleBlock
    elif geocode_namespace.memory_mode == 'geogrid':
        geocode_namespace.memory_mode = \
            isce3.core.GeocodeMemoryMode.BlocksGeogrid
    elif geocode_namespace.memory_mode == 'geogrid_and_radargrid':
        geocode_namespace.memory_mode = \
            isce3.core.GeocodeMemoryMode.BlocksGeogridAndRadarGrid
    elif (geocode_namespace.memory_mode == 'auto' or
          geocode_namespace.memory_mode is None):
        geocode_namespace.memory_mode = \
            isce3.core.GeocodeMemoryMode.Auto
    else:
        err_msg = f"ERROR memory_mode: {geocode_namespace.memory_mode}"
        raise ValueError(err_msg)

    rtc_output_type = rtc_namespace.output_type
    if rtc_output_type == 'sigma0':
        rtc_namespace.output_type = \
            isce3.geometry.RtcOutputTerrainRadiometry.SIGMA_NAUGHT
    else:
        rtc_namespace.output_type = \
            isce3.geometry.RtcOutputTerrainRadiometry.GAMMA_NAUGHT

    if rtc_namespace.input_terrain_radiometry == "sigma0":
        rtc_namespace.input_terrain_radiometry = \
            isce3.geometry.RtcInputTerrainRadiometry.SIGMA_NAUGHT_ELLIPSOID
    else:
        rtc_namespace.input_terrain_radiometry = \
            isce3.geometry.RtcInputTerrainRadiometry.BETA_NAUGHT

    if rtc_namespace.rtc_min_value_db is None:
        rtc_namespace.rtc_min_value_db = np.nan

    # Update the DEM interpolation method
    dem_interp_method = \
        cfg.groups.processing.dem_interpolation_method

    if dem_interp_method == 'biquintic':
        dem_interp_method_enum = isce3.core.DataInterpMethod.BIQUINTIC
    elif (dem_interp_method == 'sinc'):
        dem_interp_method_enum = isce3.core.DataInterpMethod.SINC
    elif (dem_interp_method == 'bilinear'):
        dem_interp_method_enum = isce3.core.DataInterpMethod.BILINEAR
    elif (dem_interp_method == 'bicubic'):
        dem_interp_method_enum = isce3.core.DataInterpMethod.BICUBIC
    elif (dem_interp_method == 'nearest'):
        dem_interp_method_enum = isce3.core.DataInterpMethod.NEAREST
    else:
        err_msg = ('ERROR invalid DEM interpolation method:'
                   f' {dem_interp_method}')
        raise ValueError(err_msg)

    cfg.groups.processing.dem_interpolation_method_enum = \
        dem_interp_method_enum


def get_rtc_s1_parser():
    '''Initialize YamlArgparse class and parse CLI arguments for OPERA RTC.
    '''
    parser = argparse.ArgumentParser(description='',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('run_config_path',
                        type=str,
                        nargs='?',
                        default=None,
                        help='Path to run config file')

    parser.add_argument('--log',
                        '--log-file',
                        dest='log_file',
                        type=str,
                        help='Log file')

    parser.add_argument('--full-log-format',
                        dest='full_log_formatting',
                        action='store_true',
                        default=False,
                        help='Enable full formatting of log messages')

    return parser


if __name__ == "__main__":
    '''Run geocode rtc workflow from command line'''
    # load arguments from command line
    parser  = get_rtc_s1_parser()
    
    # parse arguments
    args = parser.parse_args()

    # create logger
    create_logger(args.log_file, args.full_log_formatting)

    # Get a runconfig dict from command line argumens
    cfg = RunConfig.load_from_yaml(args.run_config_path, 'rtc_s1')

    _load_parameters(cfg)

    # Run geocode burst workflow
    run(cfg)
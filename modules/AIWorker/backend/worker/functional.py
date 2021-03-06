'''
    Routines responsible for the AI model update:
        1. Assemble data and organize
        2. Call the model's anonymous function (train, inference, rank)
        3. Collect the results, assemble, push to database and return
           status

    Also takes care of exceptions and errors, so that the model functions
    don't have to do it.

    The routines defined in this class may be called by multiple modules in
    different ways:
    1. In the first step, an 'AIController' instance receiving a request
       forwards it via a distributed task queue (e.g. Celery).
    2. The task queue then delegates it to one or more connected consumers
       (resp. 'AIWorker' instances).
    3. Each AIWorker instance has a task queue server running and listens to
       jobs distributed by the AIController instance. Whenever a job comes in,
       it calls the very same function the AIController instance delegated and
       processes it (functions below).

    2019-20 Benjamin Kellenberger
'''

import base64
import numpy as np
from celery import current_task, states
import psycopg2
from psycopg2 import sql
from util.helpers import current_time, array_split
from constants.dbFieldNames import FieldNames_annotation, FieldNames_prediction



def __get_message_fun(project, cumulatedTotal=None, epoch=None, numEpochs=None):
    def __on_message(state, message, done=None, total=None):
        meta = {
            'project': project,
            'epoch': epoch
        }
        if (isinstance(done, int) or isinstance(done, float)) and \
            (isinstance(total, int) or isinstance(total, float)):
            trueTotal = total
            if isinstance(cumulatedTotal, int) or isinstance(cumulatedTotal, float):
                trueTotal = max(trueTotal, total)
            meta['done'] = min(done, trueTotal)
            meta['total'] = max(done, trueTotal)

        message_combined = ''
        if isinstance(epoch, int) and isinstance(numEpochs, int):
            message_combined += f'[Epoch {epoch}/{numEpochs}] '

        if isinstance(message, str):
            message_combined += message
        
        if len(message_combined):
            meta['message'] = message_combined
        current_task.update_state(
            state=state,
            meta=meta
        )
    return __on_message


def __load_model_state(project, dbConnector):
    # load model state from database
    queryStr = sql.SQL('''
        SELECT query.statedict, query.id FROM (
            SELECT statedict, id, timecreated
            FROM {}
            ORDER BY timecreated DESC NULLS LAST
            LIMIT 1
        ) AS query;
    ''').format(sql.Identifier(project, 'cnnstate'))
    result = dbConnector.execute(queryStr, None, numReturn=1)     #TODO: issues Celery warning if no state dict found
    if not len(result):
        # force creation of new model
        stateDict = None
        stateDictID = None

    else:
        # extract
        stateDict = result[0]['statedict']
        stateDictID = result[0]['id']

    return stateDict, stateDictID



def __load_metadata(project, dbConnector, imageIDs, loadAnnotations):

    # prepare
    meta = {}

    # label names
    labels = {}
    queryStr = sql.SQL(
        'SELECT * FROM {};').format(sql.Identifier(project, 'labelclass'))
    result = dbConnector.execute(queryStr, None, 'all')
    if len(result):
        for r in result:
            labels[r['id']] = r
    meta['labelClasses'] = labels

    # image data
    imageMeta = {}
    if len(imageIDs):
        queryStr = sql.SQL(
            'SELECT * FROM {} WHERE id IN %s').format(sql.Identifier(project, 'image'))
        result = dbConnector.execute(queryStr, (tuple(imageIDs),), 'all')
        if len(result):
            for r in result:
                imageMeta[r['id']] = r

    # annotations
    if loadAnnotations and len(imageIDs):
        # get project's annotation type
        result = dbConnector.execute(sql.SQL('''
                SELECT annotationType
                FROM aide_admin.project
                WHERE shortname = %s;
            '''),
            (project,),
            1)
        annoType = result[0]['annotationtype']

        fieldNames = list(getattr(FieldNames_annotation, annoType).value)
        queryStr = sql.SQL('''
            SELECT id AS annotationID, image, {fieldNames} FROM {id_anno} AS anno
            WHERE image IN %s;
        ''').format(
            fieldNames=sql.SQL(', ').join([sql.SQL(f) for f in fieldNames]),
            id_anno=sql.Identifier(project, 'annotation'))
        result = dbConnector.execute(queryStr, (tuple(imageIDs),), 'all')
        if len(result):
            for r in result:
                if not 'annotations' in imageMeta[r['image']]:
                    imageMeta[r['image']]['annotations'] = []
                imageMeta[r['image']]['annotations'].append(r)
    meta['images'] = imageMeta

    return meta


def __get_ai_library_names(project, dbConnector):
    model_library, alcriterion_library = None, None
    try:
        queryStr = sql.SQL('''
            SELECT ai_model_library, ai_alcriterion_library
            FROM "aide_admin".project
            WHERE shortname = %s;
        ''')
        result = dbConnector.execute(queryStr, (project,), 1)
        model_library = result[0]['ai_model_library']
        alcriterion_library = result[0]['ai_alcriterion_library']
    finally:
        return model_library, alcriterion_library


def _call_train(project, imageIDs, epoch, numEpochs, subset, trainingFun, dbConnector, fileServer):
    '''
        Initiates model training and maintains workers, status and failure
        events.

        Inputs:
        - imageIDs: a list of image UUIDs the model should be trained on. Note that the remaining
                    metadata (labels, class definitions, etc.) will be loaded here.
        
        Function then performs sanity checks and forwards the data to the AI model's anonymous
        'train' function, together with some helper instances (a 'Database' instance as well as a
        'FileServer' instance TODO for the model to access more data, if needed).

        Returns:
        - modelStateDict: a new, updated state dictionary of the model as returned by the AI model's
                          'train' function.
        - TODO: more?
    '''

    print(f'[{project}] Epoch {epoch}: Initiated training...')
    update_state = __get_message_fun(project, len(imageIDs), epoch, numEpochs)


    # load model state
    update_state(state='PREPARING', message=f'[Epoch {epoch}] loading model state')
    try:
        stateDict, _ = __load_model_state(project, dbConnector)
    except Exception as e:
        print(e)
        raise Exception(f'[Epoch {epoch}] error during model state loading (reason: {str(e)})')


    # load labels and other metadata
    update_state(state='PREPARING', message=f'[Epoch {epoch}] loading metadata')
    try:
        data = __load_metadata(project, dbConnector, imageIDs, True)
    except Exception as e:
        print(e)
        raise Exception(f'[Epoch {epoch}] error during metadata loading (reason: {str(e)})')

    # call training function
    try:
        update_state(state='PREPARING', message=f'[Epoch {epoch}] initiating training')
        stateDict = trainingFun(stateDict=stateDict, data=data, updateStateFun=update_state)
    except Exception as e:
        print(e)
        raise Exception(f'[Epoch {epoch}] error during training (reason: {str(e)})')


    # commit state dict to database
    try:
        update_state(state='FINALIZING', message=f'[Epoch {epoch}] saving model state')
        model_library, alcriterion_library = __get_ai_library_names(project, dbConnector)
        queryStr = sql.SQL('''
            INSERT INTO {} (stateDict, partial, model_library, alcriterion_library)
            VALUES( %s, %s, %s, %s )
        ''').format(sql.Identifier(project, 'cnnstate'))
        dbConnector.execute(queryStr, (psycopg2.Binary(stateDict), subset, model_library, alcriterion_library), numReturn=None)
    except Exception as e:
        print(e)
        raise Exception(f'[Epoch {epoch}] error during data committing (reason: {str(e)})')

    update_state(state=states.SUCCESS, message='trained on {} images'.format(len(imageIDs)))

    print(f'[{project}] Epoch {epoch}: Training completed successfully.')
    return



def _call_average_model_states(project, epoch, numEpochs, averageFun, dbConnector, fileServer):
    '''
        Receives a number of model states (coming from different AIWorker instances),
        averages them by calling the AI model's 'average_model_states' function and inserts
        the returning averaged model state into the database.
    '''

    print(f'[{project}] Epoch {epoch}: Initiated model state averaging...')
    update_state = update_state = __get_message_fun(project, None, epoch, numEpochs)

    # get all model states
    update_state(state='PREPARING', message=f'[Epoch {epoch}] loading model states')
    try:
        queryStr = sql.SQL('''
            SELECT stateDict, model_library, alcriterion_library FROM {} WHERE partial IS TRUE;
        ''').format(sql.Identifier(project, 'cnnstate'))
        queryResult = dbConnector.execute(queryStr, None, 'all')
    except Exception as e:
        print(e)
        raise Exception(f'[Epoch {epoch}] error during model state loading (reason: {str(e)})')

    if not len(queryResult):
        # no states to be averaged; return
        print(f'[{project}] Epoch {epoch}: No model states to be averaged.')
        update_state(state=states.SUCCESS, message=f'[Epoch {epoch}] no model states to be averaged')
        return

    # do the work
    update_state(state='PREPARING', message=f'[Epoch {epoch}] averaging models')
    try:
        modelStates = [qr['statedict'] for qr in queryResult]
        modelStates_avg = averageFun(stateDicts=modelStates, updateStateFun=update_state)
    except Exception as e:
        print(e)
        raise Exception(f'[Epoch {epoch}] error during model state fusion (reason: {str(e)})')

    # push to database
    update_state(state='FINALIZING', message=f'[Epoch {epoch}] saving model state')
    try:
        model_library = queryResult[0]['model_library']
        alcriterion_library = queryResult[0]['alcriterion_library']
    except:
        # load model library from database
        model_library, alcriterion_library = __get_ai_library_names(project, dbConnector)
    try:
        queryStr = sql.SQL('''
            INSERT INTO {} (stateDict, partial, model_library, alcriterion_library)
            VALUES ( %s )
        ''').format(sql.Identifier(project, 'cnnstate'))
        dbConnector.insert(queryStr, (modelStates_avg, False, model_library, alcriterion_library))
    except Exception as e:
        print(e)
        raise Exception(f'[Epoch {epoch}] error during data committing (reason: {str(e)})')

    # delete partial model states
    update_state(state='FINALIZING', message=f'[Epoch {epoch}] purging cache')
    try:
        queryStr = sql.SQL('''
            DELETE FROM {} WHERE partial IS TRUE;
        ''').format(sql.Identifier(project, 'cnnstate'))
        dbConnector.execute(queryStr, None, None)
    except Exception as e:
        print(e)
        raise Exception(f'[Epoch {epoch}] error during cache purging (reason: {str(e)})')

    # all done
    update_state(state=states.SUCCESS, message=f'[Epoch {epoch}] averaged {len(queryResult)} model states')

    print(f'[{project}] Epoch {epoch}: Model averaging completed successfully.')
    return



def _call_inference(project, imageIDs, epoch, numEpochs, inferenceFun, rankFun, dbConnector, fileServer, batchSizeLimit):
    '''

    '''
    print(f'[{project}] Epoch {epoch}: Initiated inference on {len(imageIDs)} images...')
    update_state = update_state = __get_message_fun(project, len(imageIDs), epoch, numEpochs)

    # get project's prediction type
    projectMeta = dbConnector.execute(sql.SQL('''
            SELECT predictionType
            FROM aide_admin.project
            WHERE shortname = %s;
        '''),
        (project,),
        1)
    predType = projectMeta[0]['predictiontype']

    # load model state
    update_state(state='PREPARING', message=f'[Epoch {epoch}] loading model state')
    try:
        stateDict, stateDictID = __load_model_state(project, dbConnector)
    except Exception as e:
        print(e)
        raise Exception(f'[Epoch {epoch}] error during model state loading (reason: {str(e)})')

    # if batch size limit specified: split imageIDs into chunks and process in smaller batches
    if isinstance(batchSizeLimit, int) and batchSizeLimit > 0:
        imageID_chunks = array_split(imageIDs, batchSizeLimit)
    else:
        imageID_chunks = [imageIDs]

    # process in batches
    for idx, imageID_batch in enumerate(imageID_chunks):
        chunkStr = f'{idx+1}/{len(imageID_chunks)}'
        print(f'Chunk {chunkStr}')

        # load remaining data (image filenames, class definitions)
        update_state(state='PREPARING', message=f'[Epoch {epoch}] loading metadata (chunk {chunkStr})')
        try:
            data = __load_metadata(project, dbConnector, imageID_batch, False)
        except Exception as e:
            print(e)
            raise Exception(f'[Epoch {epoch}] error during metadata loading (chunk {chunkStr})')

        # call inference function
        update_state(state='PREPARING', message=f'[Epoch {epoch}] starting inference (chunk {chunkStr})')
        try:
            result = inferenceFun(stateDict=stateDict, data=data, updateStateFun=update_state)
        except Exception as e:
            print(e)
            raise Exception(f'[Epoch {epoch}] error during inference (chunk {chunkStr}; reason: {str(e)})')

        # call ranking function (AL criterion)
        if rankFun is not None:
            update_state(state='PREPARING', message=f'[Epoch {epoch}] calculating priorities (chunk {chunkStr})')
            try:
                result = rankFun(data=result, updateStateFun=update_state, **{'stateDict':stateDict})
            except Exception as e:
                print(e)
                raise Exception(f'[Epoch {epoch}] error during ranking (chunk {chunkStr}, reason: {str(e)})')

        # parse result
        try:
            update_state(state='FINALIZING', message=f'[Epoch {epoch}] saving predictions (chunk {chunkStr})')
            fieldNames = list(getattr(FieldNames_prediction, predType).value)
            fieldNames.append('image')      # image ID
            fieldNames.append('cnnstate')   # model state ID
            values_pred = []
            values_img = []     # mostly for feature vectors
            # ids_img = []        # to delete previous predictions
            for imgID in result.keys():
                for prediction in result[imgID]['predictions']:

                    # if segmentation mask: encode
                    if predType == 'segmentationMasks':
                        segMask = np.array(result[imgID]['predictions'][0]['label']).astype(np.uint8)
                        height, width = segMask.shape
                        segMask = base64.b64encode(segMask.ravel()).decode('utf-8')
                        segMaskDimensions = {
                            'width': width,
                            'height': height
                        }

                    nextResultValues = []
                    # we expect a dict of values, so we can use the fieldNames directly
                    for fn in fieldNames:
                        if fn == 'image':
                            nextResultValues.append(imgID)
                            # ids_img.append(imgID)
                        elif fn == 'cnnstate':
                            nextResultValues.append(stateDictID)
                        elif fn == 'segmentationmask':
                            nextResultValues.append(segMask)
                        elif fn == 'width' or fn == 'height':
                            if predType == 'segmentationMasks':
                                nextResultValues.append(segMaskDimensions[fn])
                            elif fn in prediction:
                                nextResultValues.append(prediction[fn])
                            else:
                                nextResultValues.append(None)
                        else:
                            if fn in prediction:
                                #TODO: might need to do typecasts (e.g. UUID?)
                                nextResultValues.append(prediction[fn])

                            else:
                                # field name is not in return value; might need to raise a warning, Exception, or set to None
                                nextResultValues.append(None)
                            
                    values_pred.append(tuple(nextResultValues))

                if 'fVec' in result[imgID] and len(result[imgID]['fVec']):
                    values_img.append((imgID, psycopg2.Binary(result[imgID]['fVec']),))
        except Exception as e:
            print(e)
            raise Exception(f'[Epoch {epoch}] error during result parsing (chunk {chunkStr}, reason: {str(e)})')


        # commit to database
        try:
            if len(values_pred):
                # TODO: we do not delete old predictions anymore, to keep track of model performance over time
                # # remove previous predictions first
                # queryStr = sql.SQL('''
                #     DELETE FROM {} WHERE image IN %s;
                # ''').format(sql.Identifier(project, 'prediction'))
                # dbConnector.insert(queryStr, (ids_img,))
                
                queryStr = sql.SQL('''
                    INSERT INTO {id_pred} ( {fieldNames} )
                    VALUES %s;
                ''').format(
                    id_pred=sql.Identifier(project, 'prediction'),
                    fieldNames=sql.SQL(',').join([sql.SQL(f) for f in fieldNames]))
                dbConnector.insert(queryStr, values_pred)

            if len(values_img):
                queryStr = sql.SQL('''
                    INSERT INTO {} ( id, fVec )
                    VALUES %s
                    ON CONFLICT (id) DO UPDATE SET fVec = EXCLUDED.fVec;
                ''').format(sql.Identifier(project, 'image'))
                dbConnector.insert(queryStr, values_img)
        except Exception as e:
            print(e)
            raise Exception(f'[Epoch {epoch}] error during data committing (chunk {chunkStr}, reason: {str(e)})')
    
    update_state(state=states.SUCCESS, message='predicted on {} images'.format(len(imageIDs)))

    print(f'[{project}] Epoch {epoch}: Inference completed successfully.')
    return
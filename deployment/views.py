#coding:utf-8

import os
import json
import string
import urllib
import chardet
from datetime import datetime

from django.core.cache import cache
from django.db.models import Q

from django.http import HttpResponse, Http404
from django.http import HttpResponseRedirect
from django.template import RequestContext
from django.shortcuts import render_to_response

from django.contrib.auth.models import User
from django.contrib.auth import logout, authenticate, login
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.core.servers.basehttp import FileWrapper

from deployment.deploysetting import *
from deployment.models import *
from deployment.forms import *
from deployment.logutil import *
from deployment.deployimpl import *
from deployment.sftpconn import *


def check_version_and_env(request):
    params = {
        'version': VERSION,
        'environment': ENVIRONMENT,
    }
    return HttpResponse(json.dumps(params))

@login_required
def main_page(request):
    cur_lock = _check_lock()
    params = RequestContext(request, {
        'user': request.user,
        'curLock': cur_lock
    })
    return render_to_response('main_page.html', params)

@login_required
def user_page(request, username):
    try:
        user = User.objects.get(username = username)
    except:
        raise Http404('Requested user not found.')
    
    params = RequestContext(request, {
        'username': user.username,
    })
    return render_to_response('user_page.html', params)

@login_required
def logout_page(request):
    logout(request)
    return HttpResponseRedirect('/login/')

def register_page(request):
    if request.method == 'POST':
        form = RegistrationForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password1')
            User.objects.create_user(
                username = username, 
                password = password,
                email = form.cleaned_data.get('email')
            )
            user = authenticate(username = username, password = password)
            login(request, user)
            return HttpResponseRedirect('/')
    else:
        form = RegistrationForm()
    params = RequestContext(request, {
        'form': form,
    })
    return render_to_response('registration/register.html', params)


########业务相关########
@login_required
def deploy_init_option_page(request):
    error_msg = None
    if request.POST:
        proj_id_str = request.POST.get('projId')
        deploy_type = request.POST.get('deployType')
        version = request.POST.get('version')
        cur_lock = _check_lock()
        if not proj_id_str or not deploy_type or not version:
            error_msg = '输入参数有误'
        elif cur_lock:
            return HttpResponseRedirect('/')
        else:
            proj_id = int(proj_id_str)
            project = Project.objects.get(pk = proj_id)
            try:
                cur_server = WEB_SERVER[project.name][0]
            except:
                cur_server = 'NONE'
            _params = {
                'project': project,
                'deployType': deploy_type,
                'version': version,
                'curServer': cur_server,
            }
            (record, lock) = _before_deploy_project(request, _params)
            _params['record'] = record
            _params['lock'] = lock
            #读取日志
            if deploy_type == DeployItem.WAR:
                folder_path = _generate_upload_folder_path(project.name, version);
                readme_content = _get_readme_content(folder_path)
                _params['readmeContent'] = readme_content
            params = RequestContext(request, _params)
            return render_to_response('deploy_project_page.html', params)
    params = RequestContext(request, {
        'error_msg': error_msg,
        'projects': Project.objects.all()
    })
    return render_to_response('deploy_init_option_page.html', params)

def _check_lock():
    locks = DeployLock.objects.filter(is_locked = True)
    if len(locks) >= 1:
        lock = locks[0]
        period = datetime.now() - lock.locked_time
        if period.days * 24 * 3600 + period.seconds <= LOCK_TIMEOUT_PERIOD:
            return lock
        else:
            lock.is_locked = False
            lock.save()
    return None

def _before_deploy_project(request, params):
    # create DeployRecord
    # create DeployLock and build the relationship between it and DeployRecord
    project = params.get('project');
    record = DeployRecord(
        user = request.user,
        project = project,
        create_time = datetime.now(),
        status = DeployRecord.PREPARE
    )
    record.save()
    lock = DeployLock(
        user = request.user,
        deploy_record = record,
        is_locked = True,
        locked_time = datetime.now()
    )
    lock.save()
    return (record, lock)

@login_required
def unlock_deploy(request):
    curUser = request.user
    curLock = _check_lock()
    # 本人或者超级管理员有权解锁
    if curLock and (curUser.is_superuser or curUser.id == curLock.user.id):
        curLock.is_locked = False
        curLock.save()
    return HttpResponseRedirect('/')


@login_required
def deploy_record_list_page(request, page_num=1):
    projects = Project.objects.all()
    conditions = []
    query_params = {}
    num_per_page = 20
    
    if request.POST:
        username = request.POST.get('username')
        if username:
            query_params['username'] = username
            conditions.append(Q(user__username__icontains = username))
        proj_id = int(request.POST.get('project'))
        if proj_id:
            query_params['project'] = proj_id
            conditions.append(Q(project__id = proj_id))
        version = request.POST.get('version')
        if version:
            query_params['version'] = version
            conditions.append(Q(deploy_item__version = version))
        deploy_type = request.POST.get('deployType')
        if deploy_type:
            query_params['deploy_type'] = deploy_type
            conditions.append(Q(deploy_item__deploy_type = deploy_type))
        start_time = request.POST.get('startTime')
        if start_time:
            query_params['start_time'] = start_time
            conditions.append(Q(create_time__gte = start_time))
        end_time = request.POST.get('endTime')
        if end_time:
            query_params['end_time'] = end_time
            conditions.append(Q(create_time__lte = end_time))
    query_result = DeployRecord.objects.filter(*conditions).order_by('-id')
    paged_records = Paginator(query_result, num_per_page);
    records = paged_records.page(page_num)
    for record in records:
        record.formated_create_time = record.create_time.strftime('%Y-%m-%d %H:%M:%S')
    
    query_params['curPage'] = page_num
    query_params['numPerPage'] = num_per_page
    query_params['totalPage'] = paged_records.num_pages
    params = RequestContext(request, {
        'projects': projects,
        'records': records,
        'query_params': query_params,
    })
    return render_to_response('deploy_record_list_page.html', params) 

@login_required
def deploy_record_detail_page(request, record_id):
    record = DeployRecord.objects.get(pk = record_id)
    item = record.deploy_item
    readme = ''
    file_list_content = ''
    if item.deploy_type == DeployItem.PATCH:
        folder_path = item.folder_path + trim_compress_suffix(item.file_name) + '/'
        readme = _get_readme_content(folder_path)
        file_list = _get_file_list(folder_path)
        file_list_content = '\r\n'.join(file_list)
    elif item.deploy_type == DeployItem.WAR:
        readme = _get_readme_content(item.folder_path)
        file_list_content = item.file_name
        
    params = RequestContext(request, {
        'record': record,
        'readme': readme,
        'fileList': file_list_content,
    })
    return render_to_response('deploy_record_detail_page.html', params)

# 上传readme只限版本发布
@login_required
def upload_readme(request):
    params = {}
    if request.POST and request.FILES:
        proj_name = request.POST.get('projName')
        version =request.POST.get('version')
        folderpath = _generate_upload_folder_path(proj_name, version)
        if not os.path.isdir(folderpath):
            os.makedirs(folderpath)
        readme_file = request.FILES.get('readmeField')
        destination = open(folderpath + 'readme.txt', 'wb+')
        for chunk in readme_file.chunks():
            destination.write(chunk)
        destination.close()
        readme_content = _get_readme_content(folderpath)
        params['isSuccess'] = True
        params['readmeContent'] = readme_content
    else:
        params['isSuccess'] = False
    return HttpResponse(json.dumps(params))

@login_required
def upload_deploy_item(request):
    params = {}
    if request.POST and request.FILES:
        flag = True ##也许下面应该try一下
        proj_id = int(request.POST.get('projId'))
        record_id = int(request.POST.get('recordId'))
        version = string.strip(request.POST.get('version') or '')
        deploy_type = request.POST.get('deployType')
        
        project = Project.objects.get(pk = proj_id)
        
        deploy_item_file = request.FILES.get('deployItemField')
        filename = deploy_item_file.name
        size = deploy_item_file.size
        
        folderpath = _generate_upload_folder_path(project.name, version)
        if not os.path.isdir(folderpath):
            os.makedirs(folderpath)
        destination = open(folderpath + filename, 'wb+')
        for chunk in deploy_item_file.chunks():
            destination.write(chunk)
        destination.close()
        
        items = DeployItem.objects.filter(file_name__exact = filename, version__exact = version)
        item = (items and len(items) > 0) and items[0] or None
        now_time = datetime.now()
        if not item:
            item = DeployItem(
                user = request.user,
                project = project,
                version = version,
                deploy_type = deploy_type,
                file_name = filename,
                folder_path = folderpath,
                create_time = now_time,
                update_time = now_time)
        else:
            item.update_time = now_time
        item.save()
        
        record = DeployRecord.objects.get(pk = record_id)
        if record:
            record.status = DeployRecord.UPLOADED
            record.deploy_item = item;
            record.save()
            
        params['filename'] = filename
        params['size'] = size
        params['isSuccess'] = flag
    else:
        params['isSuccess'] = False
    return HttpResponse(json.dumps(params))

@login_required
def decompress_item(request):
    params = {}
    if request.POST and request.POST.get('recordId'):
        record_id_str = request.POST.get('recordId')
        record = DeployRecord.objects.get(pk = int(record_id_str))
        item = record.deploy_item
        flag = False
        if item and item.file_name.endswith('.zip'):
            unziped_folder = item.folder_path + trim_compress_suffix(item.file_name) + '/'
            if os.path.isdir(unziped_folder):
                os.system('rm -rf ' + unziped_folder)
            flag = 0 == os.system('unzip -o ' + item.folder_path + item.file_name + ' -d ' + item.folder_path)
        if flag:
            params['isSuccess'] = True
            params['readme'] = _get_readme_content(unziped_folder)
            params['fileList'] = _get_file_list(unziped_folder)
                
    if not params.has_key('isSuccess'):
        params['isSuccess'] = False
    return HttpResponse(json.dumps(params))

@login_required
def start_rollback(request):
    # 检查是否拥有当前的发布锁
    cur_lock = _check_lock()
    if not cur_lock or cur_lock.user.id != request.user.id:
        params = {
            'beginDeploy': False,
            'errorMsg': '在发布页面停留时间过长，发布锁已超时'
        }
        return HttpResponse(json.dumps(params))
    params = None
    error_msg = ''
    if request.POST and request.POST.get('recordId'):
        record_id_str = request.POST.get('recordId')
        record = DeployRecord.objects.get(pk = int(record_id_str))
        deploy_type = request.POST.get('deployType')
        if deploy_type != DeployItem.PATCH:
            error_msg = '只有补丁的发布可以回滚'
        elif record.status != DeployRecord.SUCCESS \
            and record.status != DeployRecord.FAILURE \
            and record.status != DeployRecord.ROLLBACK:
            error_msg = '只有完成后的发布可以回滚'
        elif cache.get('log_is_writing_' + record_id_str):
            error_msg = '发布仍在继续中...'
        else:
            log_reader = LogReader()
            cache.set('log_reader_' + record_id_str, log_reader, 300)
            deployer = Deployer(record = record, direct = 'rollback')
            deployer.start()
            record.status = DeployRecord.ROLLBACKING
            record.save()
            params = {
                'beginDeploy': True,
            }
    if not params:
        params = {
            'beginDeploy': False,
            'errorMsg': error_msg
        }
    return HttpResponse(json.dumps(params))

@login_required
def start_deploy(request):
    # 检查是否拥有当前的发布锁
    cur_lock = _check_lock()
    if not cur_lock or cur_lock.user.id != request.user.id:
        params = {
            'beginDeploy': False,
            'errorMsg': '在发布页面停留时间过长，发布锁已超时'
        }
        return HttpResponse(json.dumps(params))
    # 发布前需要检查状态，至少为uploaded
    params = None
    error_msg = ''
    if request.POST and request.POST.get('recordId'):
        record_id_str = request.POST.get('recordId')
        record = DeployRecord.objects.get(pk = int(record_id_str))
        
        # 版本发布需要web.xml
        if record.status == DeployRecord.PREPARE:
            error_msg = '尚未上传文件'
        elif cache.get('log_is_writing_' + record_id_str):
            error_msg = '发布仍在继续中...'
        else:
            log_reader = LogReader()
            cache.set('log_reader_' + record_id_str, log_reader, 300)
            deployer = Deployer(record = record)
            deployer.start()
            record.status = DeployRecord.DEPLOYING
            record.save()
            params = {
                'beginDeploy': True,
            }
    if not params:
        params = {
            'beginDeploy': False,
            'errorMsg': error_msg
        }
    return HttpResponse(json.dumps(params))

@login_required
def read_deploy_log_on_realtime(request):
    record_id_str = request.GET.get('recordId')
    params = {}
    if record_id_str:
        # log_reader在start_deploy的时候生成并放入cache
        log_reader_key = 'log_reader_' + record_id_str
        log_reader = cache.get(log_reader_key)
        if log_reader:
            params['logInfo'] = log_reader.read_lines()
            params['isFinished'] = False
            if cache.get('log_is_writing_' + record_id_str):
                cache.set(log_reader_key, log_reader, 300)
            else:
                cache.delete(log_reader_key)
            return HttpResponse(json.dumps(params))
    deploy_result_key = 'deploy_result_' + record_id_str
    deploy_result = cache.get(deploy_result_key)
    cache.delete(deploy_result_key)
    record = DeployRecord.objects.get(pk = int(record_id_str))
    if deploy_result:
        record.status = record.status == DeployRecord.ROLLBACKING and DeployRecord.ROLLBACK or DeployRecord.SUCCESS
    else:
        record.status = DeployRecord.FAILURE
#    record.status = deploy_result and DeployRecord.SUCCESS or DeployRecord.FAILURE
    record.save()
    params['isFinished'] = True
    params['deployResult'] = deploy_result
    return HttpResponse(json.dumps(params))

def _generate_upload_folder_path(proj_name, version):
    return get_target_folder(proj_name, version)

def _get_readme_content(folder_path):
    readme_path = folder_path + 'readme.txt'
    if os.path.isfile(readme_path):
        readme_f = open(readme_path, 'r')
        lines = readme_f.readlines()
        readme_f.close()
        readme =  ''.join(lines)
    else:
        readme = ''
    return convert2utf8(readme)

def _get_file_list(root_path, depth = 0):
    if not os.path.isdir(root_path):
        return []
    f_list = []
    if not root_path.endswith('/'):
        root_path += '/'
    f_dirs = os.listdir(root_path)
    for f in f_dirs:
        f_path = root_path + f
        if os.path.isdir(f_path):
            f_list += _get_file_list(f_path, depth + 1)
        elif os.path.isfile(f_path):
            f_list.append(f_path)
    if depth == 0:
        for i in range(len(f_list)):
            s =  f_list[i][len(root_path):]
            s = string.replace(s, '/', '.')
            f_list[i] = s
        f_list.sort()
    return f_list

def convert2utf8(content):
    if not content:
        return content
    encode_manner = chardet.detect(content)
    if not encode_manner:
        return content
    if encode_manner['confidence'] < 0.6:
        return content
    if encode_manner['encoding'] == 'utf-8':
        return content
    return content.decode(encode_manner['encoding']).encode('utf8')


# 线上文件远程下载
def file_node_cmp(n1, n2):
    if n1['isParent'] ^ n2['isParent']:
        return n1['isParent'] and -1 or 1
    elif n1['isParent'] and n2['isParent']:
        return cmp(n1['name'], n2['name'])
    else:
        suffix1 = n1['name'].split('.')[-1]
        suffix2 = n2['name'].split('.')[-1]
        suffix_cmp = cmp(suffix1, suffix2)
        if suffix_cmp:
            return suffix_cmp
        return cmp(n1['name'], n2['name'])

@login_required
def get_project_file_nodes(request, project_id = 0, server_idx = 0,  node_id = '0'):
    server_idx = int(server_idx)
    file_path = request.GET.get('file_path')
    if not file_path:
        file_path = ''
    project = Project.objects.get(pk = project_id)
    filename_arr = get_dirlist_from_ftp(file_path = file_path, project = project, server_idx = server_idx)
    nodes = []
    if filename_arr is not None:
        index = 0
        suffix_regx = re.compile(r'\.\D+$')
        for name in filename_arr:
            node = {
                'id': node_id + '_' + unicode(index),
                'name': name,
                'file_path': file_path + '/' + name,
                'isParent': not suffix_regx.search(name)
            }
            index += 1
            nodes.append(node)
        nodes.sort(cmp=file_node_cmp, key=None, reverse=False)
    return HttpResponse(json.dumps(nodes))

@login_required
def download_project_file(request, project_id = 0, server_idx = 0):
    if request.GET:
        file_path = request.GET.get('file_path')
        file_name = request.GET.get('file_name')
        project = Project.objects.get(pk = project_id)
        local_path = get_file_from_ftp(file_path = file_path, project = project, server_idx = server_idx)
        if local_path:
            wrapper = FileWrapper(file(local_path), blksize = 1048576)
            response = HttpResponse(wrapper, content_type='text/plain')
            response['Content-Length'] = os.path.getsize(local_path)
            response['Content-Disposition'] = 'attachment; filename=' + file_name
            return response
    return HttpResponse('')

# 编辑线上文件
@login_required
def show_online_file(request):
    
    params = RequestContext(request, {
        'projects': Project.objects.all(),
        'serverInfo': json.dumps(WEB_SERVER),
    });
    return render_to_response('show_online_file.html', params);

@login_required
def open_online_file(request, project_id = 0, server_idx_list = ''):
    params = {
        'isError': True
    }
    if project_id == 0 or server_idx_list.strip() == '':
        pass
    elif request.GET:
        file_path = request.GET.get('file_path')
        file_name = request.GET.get('file_name')
        server_idx_arr = server_idx_list.split('_')
        project = Project.objects.get(pk = project_id)
        local_path = get_file_from_ftp(file_path = file_path, project = project, server_idx = server_idx_arr[0])
        f = open(local_path, 'r');
        lines = f.readlines();
        f.close()
        content = ''.join(lines);
        params['isError'] = False
        params['project'] = project
        params['content'] = content
        params['filePath'] = file_path
        params['fileName'] = file_name
        params['serverIdxList'] = server_idx_list
        params['serverInfo'] = json.dumps(WEB_SERVER);

    return render_to_response('edit_online_file.html', RequestContext(request, params));

@login_required
def rename_online_file(request, project_id = 0, server_idx_list = ''):
    params = {
        'isSuccess': False
    }
    if project_id == 0 or server_idx_list.strip() == '':
        pass
    elif request.POST:
        try:
            parent_folder_path = request.POST.get('parent_folder_path')
            old_file_name = request.POST.get('old_file_name')
            new_file_name = request.POST.get('new_file_name')
            server_idx_arr = server_idx_list.split('_')
            project = Project.objects.get(pk = project_id)
            result_msg_arr = ['对文件 [ ' + str(old_file_name) + ' ] 的重命名结果:\n']
            for idx in server_idx_arr:
                flag = rename_file_on_ftp(
                    parent_folder_path = parent_folder_path, 
                    old_file_name = old_file_name, 
                    new_file_name = new_file_name,
                    project = project, server_idx = idx)
                result_msg_arr.append(WEB_SERVER[project.name][int(idx)] + (flag and ' 重命名成功!' or ' 重命名失败!'))
            result_msg = '\n'.join(result_msg_arr)
            params['isSuccess'] = True
            params['resultMsg'] = result_msg
        except:
            pass
    
    return HttpResponse(json.dumps(params))

@login_required
def backup_online_file(request, project_id = 0, server_idx_list = ''):
    params = {
        'isSuccess': False
    }
    if project_id == 0 or server_idx_list.strip() == '':
        pass
    elif request.POST:
        try:
            parent_folder_path = request.POST.get('parent_folder_path')
            file_name = request.POST.get('file_name')
            ts = unicode(int(time.time() * 1000))
            backup_file_name = file_name + '.' + ts + '.bak'
            server_idx_arr = server_idx_list.split('_')
            project = Project.objects.get(pk = project_id)
            result_msg_arr = ['对文件 [ ' + str(file_name) + ' ] 的备份结果:\n']
            for idx in server_idx_arr:
                flag = backup_file_on_ftp(parent_folder_path = parent_folder_path,
                    file_name = file_name,
                    backup_file_name = backup_file_name,
                    project = project,
                    server_idx = idx)
                result_msg_arr.append(WEB_SERVER[project.name][int(idx)] + (flag and ' 备份成功!' or ' 备份失败!'))
            result_msg_arr.append('备份文件的文件名为 [' + str(backup_file_name) + ' ]')
            result_msg = '\n'.join(result_msg_arr)
            params['isSuccess'] = True
            params['resultMsg'] = result_msg
        except:
            pass
        
    return HttpResponse(json.dumps(params))

@login_required
def save_file_from_online_editor(request, project_id = 0, server_idx_list = ''):
    params = {
        'isSuccess': False
    }
    if project_id == 0 or server_idx_list.strip() == '':
        pass
    elif request.POST:
        try:
            file_path = request.POST.get('file_path')
            file_name = request.POST.get('file_name')
            file_content = request.POST.get('file_content').encode('utf8')
            server_idx_arr = server_idx_list.split('_')
            project = Project.objects.get(pk = project_id)
            localpath = write_content_to_localfile(file_content = file_content, file_name = file_name)
            result_msg_arr = ['对文件 [ ' + str(file_name) + ' ] 的保存结果:\n']
            for idx in server_idx_arr:
                flag = upload_file_to_ftp(local_file_path = localpath, ftp_file_path = file_path, project = project, server_idx = idx, overwrite = True)
                result_msg_arr.append(WEB_SERVER[project.name][int(idx)] + (flag and ' 保存成功!' or ' 保存失败!'))
            result_msg = '\n'.join(result_msg_arr)
            params['isSuccess'] = True
            params['resultMsg'] = result_msg
        except:
            pass
    
    return HttpResponse(json.dumps(params));
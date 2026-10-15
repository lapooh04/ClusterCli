#!/usr/bin/python
import openshift_client as oc

print('OpenShift client version: {}'.format(oc.get_client_version()))
print('OpenShift server version: {}'.format(oc.get_server_version()))

# Set a project context for all inner `oc` invocations and limit execution to 10 minutes
with oc.project('openshift-infra'), oc.timeout(10 * 60):
    # Print the list of qualified pod names (e.g. ['pod/xyz', 'pod/abc', ...]  in the current project
    print('Found the following pods in {}: {}'.format(oc.get_project_name(), oc.selector('pods').qnames()))

    # Read in the current state of the pod resources and represent them as python objects
    for pod_obj in oc.selector('pods').objects():

        # The APIObject class exposes several convenience methods for interacting with objects
        print('Analyzing pod: {}'.format(pod_obj.name()))
        pod_obj.print_logs(timestamps=True, tail=15)

        # If you need access to the underlying resource definition, get a Model instance for the resource
        pod_model = pod_obj.model

        # Model objects enable dot notation and allow you to navigate through resources
        # to an arbitrary depth without checking if any ancestor elements exist.
        # In the following example, there is no need for boilerplate like:
        #    `if .... 'ownerReferences' in pod_model['metadata'] ....`
        # Fields that do not resolve will always return oc.Missing which 
        # is a singleton and can also be treated as an empty dict.
        for owner in pod_model.metadata.ownerReferences:  # ownerReferences == oc.Missing if not present in resource
            # elements of a Model are also instances of Model or ListModel
            if owner.kind is not oc.Missing:  # Compare as singleton
                print('  pod owned by a {}'.format(owner.kind))  # e.g. pod was created by a StatefulSet



import logging
import math
import os
import sys

from configobj import ConfigObj
from openmm.unit import kelvin, kilocalories_per_mole

from gibbs_sampling import pairwise_independence_sampling
from local_openmm_transport_sync import LocalOpenMMTransport
from ommreplica import OMMReplicaATM
from ommsystem import OMMSystemAmberRBFE
from ommworker_sync import OMMWorkerATM


class sync_re:
    """
    Class to set up and run asynchronous file-based RE calculations
    """
    logging.config.fileConfig(os.path.join(os.path.dirname(__file__), "utils/logging.conf"))

    def __init__(self, config_file, options):
        self._setLogger()

        self.command_file = config_file

        self.jobname = os.path.splitext(os.path.basename(config_file))[0]
        self.config = ConfigObj(self.command_file)

        self._checkInput()
        self._printStatus()

    def _setLogger(self):
        self.logger = logging.getLogger("async_re")

    def replicas_to_exchange(self):
        # Return a list of replica that completed at least one cycle.
        return [k for k in range(self.nreplicas) if self.status[k]['cycle_current'] > 1]

    def states_to_exchange(self):
        # Return a list of state ids of replicas that completed at least one cycle.
        return [self.status[k]['stateid_current'] for k in self.replicas_to_exchange()]

    def _printStatus(self):
        """Print a report of the input parameters."""
        self.logger.info("ASyncRE-OpenMM, Version")
        self.logger.info("command_file = %s", self.command_file)
        self.logger.info("jobname = %s", self.jobname)
        self.logger.info("Keywords:")
        for k,v in self.config.iteritems():
            self.logger.info("%s: %s", k, v)

    def _checkInput(self):
        """
        Check that required parameters are specified. Parse these and other
        optional settings.
        """
        # Required Options
        #
        # basename for the job
        self.basename = self.config.get('BASENAME')
        assert self.basename, 'BASENAME needs to be specified'

        # number of replicas (may be determined by other means)
        self.nreplicas = None

        # verbose printing
        if self.config.get('VERBOSE').lower() == 'yes':
            self.verbose = True
            if self.logger:
                self.logger.setLevel(logging.DEBUG)
        else:
            self.verbose = False

    def setupJob(self):
        # create status table
        self.status = [{'stateid_current': k, 'cycle_current': 1} for k in range(self.nreplicas)]
        for replica in self.openmm_replicas:
            self.status[replica._id]['cycle_current'] = replica.get_cycle()
            self.status[replica._id]['stateid_current'] = replica.get_stateid()
            self.logger.info("Replica %d Cycle %d Stateid %d" % (replica._id, self.status[replica._id]['cycle_current'], self.status[replica._id]['stateid_current']))

        self.updateStatus()

    def scheduleJobs(self):

        max_samples = None
        assert self.config.get('MAX_SAMPLES'), "MAX_SAMPLES has to be specified"
        max_samples = int(self.config.get('MAX_SAMPLES'))

        # TODO: restart properly
        num_sims = max_samples * len(self.openmm_replicas)
        for isample in range(max_samples):
            for irepl, replica in enumerate(self.openmm_replicas):
                self.logger.info(f"Simulation: sample {isample}, replica {irepl}")
                self._launchReplica(irepl, self.status[irepl]['cycle_current'])
                self.status[irepl]['cycle_current'] += 1
                assert replica.get_cycle() == self.status[irepl]['cycle_current']
                self.doExchanges()
                self.updateStatus()
                self.checkpointJob()
                self.print_status()

    def updateStatus(self):
        """Scan the replicas and update their states."""
        # self.transport.poll()
        for k in range(self.nreplicas):
            # self._updateStatus_replica(k)
            self.update_state_of_replica(k)

    def doExchanges(self):
        self.logger.info("Replica exchange")

        replicas_to_exchange = self.replicas_to_exchange()
        states_to_exchange = self.states_to_exchange()

        self.logger.debug(f"Replicas to exchange: {replicas_to_exchange}")
        self.logger.debug(f"States to exchange: {states_to_exchange}")

        if len(replicas_to_exchange) < 2:
            return 0

        # Matrix of replica energies in each state.
        # The computeSwapMatrix() function is defined by application classes
        swap_matrix = self._computeSwapMatrix(replicas_to_exchange, states_to_exchange)
        # self.logger.debug(swap_matrix)

        for repl_i in replicas_to_exchange:
            sid_i = self.status[repl_i]['stateid_current']
            curr_states = [self.status[repl_j]['stateid_current']
                           for repl_j in replicas_to_exchange]
            repl_j = pairwise_independence_sampling(repl_i,sid_i,
                                                    replicas_to_exchange,
                                                    curr_states,
                                                    swap_matrix)
            if repl_j != repl_i:
                sid_i = self.status[repl_i]['stateid_current']
                sid_j = self.status[repl_j]['stateid_current']
                self.status[repl_i]['stateid_current'] = sid_j
                self.status[repl_j]['stateid_current'] = sid_i
                self.logger.info("Replica %d new state %d" % (repl_i, sid_j))
                self.logger.info("Replica %d new state %d" % (repl_j, sid_i))


class openmm_job(sync_re):
    def __init__(self, command_file, options):
        sync_re.__init__(self, command_file, options)
        self.openmm_replicas = None
        self.stateparams = None
        self.kb = 0.0019872041*kilocalories_per_mole/kelvin

    def _setLogger(self):
        self.logger = logging.getLogger("async_re.openmm_sync_re")

    def checkpointJob(self):
        for replica in self.openmm_replicas:
            replica.save_checkpoint()

    def _launchReplica(self,replica,cycle):

        nsteps = int(self.config.get('PRODUCTION_STEPS'))
        nprnt = int(self.config.get('PRNT_FREQUENCY'))
        ntrj = int(self.config.get('TRJ_FREQUENCY'))
        assert nprnt % nsteps == 0, "PRNT_FREQUENCY must be an integer multiple of PRODUCTION_STEPS."
        assert ntrj % nsteps == 0, "TRJ_FREQUENCY must be an integer multiple of PRODUCTION_STEPS."

        job_info = {"replica": replica, "cycle": cycle, "nsteps": nsteps, "nprnt": nprnt, "ntrj": ntrj}

        return self.transport.launchJob(replica, job_info)

    def update_state_of_replica(self, repl):
        replica = self.openmm_replicas[repl]
        old_stateid, _ = replica.get_state()
        new_stateid = self.status[repl]['stateid_current']
        replica.set_state(new_stateid, self.stateparams[new_stateid])

    def _getPar(self, repl):
        _, par = self.openmm_replicas[repl].get_state()
        return par

    def _computeSwapMatrix(self, repls, states):
        """
        Compute matrix of dimension-less energies: each column is a replica
        and each row is a state so U[i][j] is the energy of replica j in state
        i.

        Note that the matrix is sized to include all of the replicas and states
        but the energies of replicas not in waiting state, or those of waiting
        replicas for states not belonging to waiting replicas list are
        undefined.
        """
        # U will be sparse matrix, but is convenient bc the indices of the
        # rows and columns will always be the same.
        U = [[ 0. for j in range(self.nreplicas)]
             for i in range(self.nreplicas)]

        n = len(repls)

        #collect replica parameters and potentials
        par = []
        pot = []
        for k in repls:
            v = self._getPot(k)
            l = self._getPar(k)
            par.append(l)
            pot.append(v)

        for i in range(n):
            repl_i = repls[i]
            for j in range(n):
                sid_j = states[j]
                #energy of replica i in state j
                U[sid_j][repl_i] = self._reduced_energy(par[j],pot[i])
        return U


class openmm_job_ATM(openmm_job):
    def _buildStates(self):
        self.stateparams = []
        for (lambd,direction,intermediate,lambda1,lambda2,alpha,u0,w0) in zip(self.lambdas,self.directions,self.intermediates,self.lambda1s,self.lambda2s,self.alphas,self.u0s,self.w0coeffs):
            for tempt in self.temperatures:
                par = {}
                par['lambda'] = float(lambd)
                par['atmdirection'] = float(direction)
                par['atmintermediate'] = float(intermediate)
                par['lambda1'] = float(lambda1)
                par['lambda2'] = float(lambda2)
                par['alpha'] = float(alpha)/kilocalories_per_mole
                par['u0'] = float(u0)*kilocalories_per_mole
                par['w0'] = float(w0)*kilocalories_per_mole
                par['temperature'] = float(tempt)*kelvin
                self.stateparams.append(par)
        return len(self.stateparams)

    def _checkInput(self):
        sync_re._checkInput(self)

        assert self.config.get('LAMBDAS'), "LAMBDAS needs to be specified"
        self.lambdas = self.config.get('LAMBDAS').split(',')
        #list of temperatures
        assert self.config.get('TEMPERATURES'), "TEMPERATURES needs to be specified"
        self.temperatures = self.config.get('TEMPERATURES').split(',')

        #flag to identify the intermediate states, typically the one at lambda=1/2
        self.intermediates = self.config.get('INTERMEDIATE').split(',')

        #direction of transformation at each lambda
        #ABFE 1 from RA to R+A, -1 from R+A to A
        #RBFE 1 from RA+B to RB+A, -1 from RB+A to RA+B
        self.directions = self.config.get('DIRECTION').split(',')

        #parameters of the softplus alchemical potential
        #lambda1 = lambda2 gives the linear potential
        self.lambda1s = self.config.get('LAMBDA1').split(',')
        self.lambda2s = self.config.get('LAMBDA2').split(',')
        self.alphas = self.config.get('ALPHA').split(',')
        self.u0s = self.config.get('U0').split(',')
        self.w0coeffs = self.config.get('W0COEFF').split(',')

        #build parameters for the lambda/temperatures combined states
        self.nreplicas = self._buildStates()

    #evaluates the softplus function
    def _softplus(self, lambda1, lambda2, alpha, u0, w0, uf):
        ee = 1.0 + math.exp(-alpha*(uf-u0))
        softplusf = lambda2 * uf + w0
        if alpha._value > 0.:
            softplusf += ((lambda2 - lambda1)/alpha) * math.log(ee)
        return softplusf

    #customized getPot to return the unperturbed potential energy
    #of the replica U0 = U - W_lambda(u)
    def _getPot(self, repl):
        replica = self.openmm_replicas[repl]
        pot = replica.get_energy()
        epot = pot['potential_energy']
        pertpot = pot['perturbation_energy']
        (stateid, par) = replica.get_state()
        # direction = par['atmdirection']
        lambda1 = par['lambda1']
        lambda2 = par['lambda2']
        alpha = par['alpha']
        u0 = par['u0']
        w0 = par['w0']
        ebias = self._softplus(lambda1, lambda2, alpha, u0, w0, pertpot)
        pot['unbiased_potential_energy'] = epot - ebias
        pot['direction'] = par['atmdirection']
        pot['intermediate'] = par['atmintermediate']
        return pot

    def _reduced_energy(self, par, pot):
        temperature = par['temperature']
        beta = 1./(self.kb*temperature)
        direction = par['atmdirection']
        lambda1 = par['lambda1']
        lambda2 = par['lambda2']
        alpha = par['alpha']
        u0 = par['u0']
        w0 = par['w0']
        state_direction = par['atmdirection']
        state_intermediate = par['atmintermediate']
        epot0 = pot['unbiased_potential_energy']
        pertpot = pot['perturbation_energy']
        replica_direction = pot['direction']
        replica_intermediate = pot['intermediate']
        if (replica_direction == state_direction) or (state_intermediate > 0 and replica_intermediate > 0):
            ebias = self._softplus(lambda1, lambda2, alpha, u0, w0, pertpot)
            return beta*(epot0 + ebias)
        else:
            #prevent exchange
            large_energy = 1.e12
            return large_energy


class openmm_job_AmberRBFE(openmm_job_ATM):
    def __init__(self, command_file, options):
        super().__init__(command_file, options)

        prmtopfile = self.basename + ".prmtop"
        crdfile = self.basename + ".inpcrd"

        if self.stateparams is None:
            self._buildStates()

        # creates openmm context objects
        ommsys = OMMSystemAmberRBFE(self.basename, self.config, prmtopfile, crdfile, self.logger)
        self.worker = OMMWorkerATM(self.basename, ommsys, self.config, logger=self.logger)

        #creates openmm replica objects
        self.openmm_replicas = []
        for i in range(self.nreplicas):
            replica = OMMReplicaATM(i, self.basename, self.worker, self.logger)
            replica.set_state(i, self.stateparams[i])
            self.openmm_replicas.append(replica)

        self.transport = LocalOpenMMTransport(self.basename, self.worker, self.openmm_replicas)

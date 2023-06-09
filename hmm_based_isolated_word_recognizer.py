import librosa
import math
import numpy as np
import scipy.signal
from scipy.special import logsumexp
import torch
import torch.nn as nn
import torch.nn.functional as F

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

class MyNet(nn.Module):
    def __init__(self):
        super(MyNet, self).__init__()
        self.conv1 = nn.Conv2d(1, 32, 5, padding=2)
        self.pool = nn.MaxPool2d(2, 2)
        self.conv2 = nn.Conv2d(32, 64, 5, padding=2)
        self.conv3 = nn.Conv2d(64, 64, 3, padding=1)
        self.conv4 = nn.Conv2d(64, 128, (1, 5))
        self.fc1 = nn.Linear(128, 128)
        self.fc2 = nn.Linear(128, 128)
        self.fc3 = nn.Linear(128, 48)
        self.sm = nn.LogSoftmax(dim=1)

    def forward(self, x):
        x = x.unsqueeze(1)
        x = self.pool(F.relu(self.conv1(x)))
        x = self.pool(F.relu(self.conv2(x)))
        x = self.pool(F.relu(self.conv3(x)))
        x = F.relu(self.conv4(x))
        x = x.view(-1, 128)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        x = self.sm(x)
        return x
    
def load_audio_to_melspec_tensor(wavpath, sample_rate=16000):
    window_size = .025
    window_stride = 0.01
    n_dft = 512
    win_length = int(sample_rate * window_size)
    hop_length = int(sample_rate * window_stride)
    y, sr = librosa.load(wavpath, sr=sample_rate)
    y = y - y.mean()
    y = np.append(y[0],y[1:]-.97*y[:-1])
    # compute mel spectrogram
    stft = librosa.stft(y, n_fft=n_dft, hop_length=hop_length,
        win_length=win_length, window=scipy.signal.hamming)
    spec = np.abs(stft)**2
    mel_basis = librosa.filters.mel(sr=sample_rate, n_fft=n_dft, n_mels=40, fmin=20)    
    melspec = np.dot(mel_basis, spec)
    logspec = librosa.power_to_db(melspec, ref=np.max)
    logspec = np.transpose(logspec)
    logspec_tensor = torch.tensor(logspec)
    return logspec_tensor

def compute_phone_likelihoods(model, logspec):
    likelihood_list = []
    with torch.no_grad():
        for j in range(6, logspec.size(0) - 5):
            inp = logspec[j-5:j+6,:].unsqueeze(0)
            output = model(inp) # output will be log probabilities over classes
            output = output - math.log(1. / 48) # subtract the logprob of the class priors (assumed to be uniform)
            likelihood_list.append(output[0])
    likelihoods = torch.transpose(torch.stack(likelihood_list, dim=1), 0, 1).numpy()
    return likelihoods

class MyHMM:
    def __init__(self, state_labels, initial_state_distribution, transition_matrix, eps=1e-200):
        self.eps = eps
        self.pi = np.log(initial_state_distribution + eps)
        self.A = np.log(transition_matrix + eps) #A_{ji} is prob of transitioning from state j to state i
        self.labels = state_labels # a list where self.labels[j] is the index of the phone label belonging to the jth state
        # print(self.labels)
        self.N_states = len(self.labels)
        
    def forward(self, state_likelihoods):
        # state_likelihoods.shape is assumed to be (N_timesteps, 48)
        # TODO: fill in
        state_likelihoods_copy = np.zeros((state_likelihoods.shape[0], self.N_states))
        for t in range(0, state_likelihoods.shape[0]):
            state_likelihoods_copy[t] = state_likelihoods[t][self.labels]
        state_likelihoods = state_likelihoods_copy
        
        # initialization_
        alpha = np.zeros((state_likelihoods.shape[0], self.N_states))
        alpha[0] = self.pi + state_likelihoods[0]

        #induction
        for t in range(1, state_likelihoods.shape[0]):
            for i in range(self.N_states):
                alpha[t, i] = logsumexp(alpha[t-1] + self.A[:,i]) + state_likelihoods[t, i]
        
        # termination

        return alpha[-1][-1]
    
    def viterbi(self, state_likelihoods):
        # state_likelihoods.shape is assumed to be (N_timesteps, 48)
        
        # get only the likelihoods for the states we care about
        state_likelihoods_copy = np.zeros((state_likelihoods.shape[0], self.N_states))
        for t in range(0, state_likelihoods.shape[0]):
            state_likelihoods_copy[t] = state_likelihoods[t][self.labels]
        state_likelihoods = state_likelihoods_copy      
        
        # initialization
        delta = np.zeros((state_likelihoods.shape[0], self.N_states))
        delta[0] = self.pi + state_likelihoods[0]
        psi = np.zeros((state_likelihoods.shape[0], self.N_states))
        psi[0] = 0
        
        # induction
        for t in range(1, state_likelihoods.shape[0]):
            for i in range(self.N_states):
                delta[t, i] = np.max(delta[t-1] + self.A[:,i]) + state_likelihoods[t, i]
                psi[t, i] = np.argmax(delta[t-1] + self.A[:,i])
        
        # termination
        q_star = np.zeros(state_likelihoods.shape[0])
        q_star[-1] = np.argmax(delta[-1])
        
        # backtracking
        for t in range(state_likelihoods.shape[0]-2, -1, -1):
            q_star[t] = psi[t+1, int(q_star[t+1])]
        
        return q_star
    
    def viterbi_transition_update(self, state_likelihoods):
        # state_likelihoods.shape is assumed to be (N_timesteps, 48)
         
        transitions_ij = np.zeros((self.N_states, self.N_states))
        out_transitions = np.zeros(self.N_states)
        
        q_star = self.viterbi(state_likelihoods)

        for t in range(0, state_likelihoods.shape[0]-1):
            transitions_ij[int(q_star[t]), int(q_star[t+1])] += 1
            out_transitions[int(q_star[t])] += 1

        self.A = np.log(transitions_ij / out_transitions[:, None] + self.eps)
        
        pass

model = MyNet()
model.load_state_dict(torch.load('lab3_AM.pt'))

lab3_data = np.load('lab3_phone_labels.npz')
phone_labels = list(lab3_data['phone_labels'])
def phones2indices(phones):
    return [phone_labels.index(p) for p in phones]

fee_HMM = MyHMM(phones2indices(['sil', 'f', 'iy', 'sil']), np.array([0.5, 0.5, 0, 0]), np.array([[.9,.1,0,0],[0,.9,.1,0],[0,0,.9,.1],[0,0,0,1]]))
pea_HMM = MyHMM(phones2indices(['sil', 'p', 'iy', 'sil']), np.array([0.5, 0.5, 0, 0]), np.array([[.9,.1,0,0],[0,.9,.1,0],[0,0,.9,.1],[0,0,0,1]]))
rock_HMM = MyHMM(phones2indices(['sil', 'r', 'aa', 'cl', 'k', 'sil']), np.array([0.5,0.5,0,0,0,0]), np.array([[.9,.1,0,0,0,0],[0,.9,.1,0,0,0],[0,0,.9,.1,0,0],[0,0,0,.9,.1,0],[0,0,0,0,.9,.1],[0,0,0,0,0,1]]))
burt_HMM = MyHMM(phones2indices(['sil', 'b', 'er', 'cl', 't', 'sil']), np.array([0.5,0.5,0,0,0,0]), np.array([[.9,.1,0,0,0,0],[0,.9,.1,0,0,0],[0,0,.9,.1,0,0],[0,0,0,.9,.1,0],[0,0,0,0,.9,.1],[0,0,0,0,0,1]]))
see_HMM = MyHMM(phones2indices(['sil', 's', 'iy', 'sil']), np.array([0.5, 0.5, 0, 0]), np.array([[.9,.1,0,0],[0,.9,.1,0],[0,0,.9,.1],[0,0,0,1]]))
she_HMM = MyHMM(phones2indices(['sil', 'sh', 'iy', 'sil']), np.array([0.5, 0.5, 0, 0]), np.array([[.9,.1,0,0],[0,.9,.1,0],[0,0,.9,.1],[0,0,0,1]]))


#Likelihood Computation
print("\nLikelihood Computation\n")
words = ['fee', 'pea', 'rock', 'burt', 'see', 'she']
hmms = [fee_HMM, pea_HMM, rock_HMM, burt_HMM, see_HMM, she_HMM]
matrix = np.zeros((6,6))
for i in range(6):
    for j in range(6):
        matrix[i,j] = hmms[i].forward(compute_phone_likelihoods(model, load_audio_to_melspec_tensor(words[j] + '.wav')))
matrix = matrix.T
for row in range(matrix.shape[0]):
    print(words[row] + " likelihoods:", matrix[row])
matrix = matrix.T
pred = np.argmax(matrix, axis=0)
print("Predications:", pred)
for i in range(6):
    print(words[i], words[pred[i]])

# Optimal State Sequence
print("\nOptimal State Sequence\n")
rock_HMM = MyHMM(phones2indices(['sil', 'r', 'aa', 'cl', 'k', 'sil']), np.array([0.5,0.5,0,0,0,0]), np.array([[.9,.1,0,0,0,0],[0,.9,.1,0,0,0],[0,0,.9,.1,0,0],[0,0,0,.9,.1,0],[0,0,0,0,.9,.1],[0,0,0,0,0,1]]))
rocks = rock_HMM.viterbi(compute_phone_likelihoods(model, load_audio_to_melspec_tensor('rock.wav')))
print("Optimal State Sequence for Rocks:", rocks)

# Viterbi Update
print("\nViterbi Update\n")
trans_before = rock_HMM.A
print("Log Likelihood Before Viterbi Update:", rock_HMM.forward(compute_phone_likelihoods(model, load_audio_to_melspec_tensor('rock.wav'))))
rock_HMM.viterbi_transition_update(compute_phone_likelihoods(model, load_audio_to_melspec_tensor('rock.wav')))
trans_after = rock_HMM.A
print("Log Likelihood After Viterbi Update:", rock_HMM.forward(compute_phone_likelihoods(model, load_audio_to_melspec_tensor('rock.wav'))))
print("The new likelihood of the rock HMM for the rock.wav file went up.")
print("The old transition matrix is:")
print(trans_before)
print("The new transition matrix is:")
print(trans_after)
print("The difference is:")
print(trans_after-trans_before)
